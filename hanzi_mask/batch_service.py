"""Texture-only desktop operations, independent of Qt and worker threads.

Processing can cache completed candidates on disk with bounded image concurrency.
Export writes a fresh run directory; replacement is PNG-only and always creates
a complete, durable backup first.
"""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Callable, Iterable

import numpy as np
from PIL import Image

from . import __version__
from .processing import Options, Result, load_image, process_image

SUPPORTED = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
Cancel = Callable[[], bool]
Progress = Callable[[int, int, Path], None]


@dataclass(frozen=True)
class InputImage:
    path: Path
    relative_path: Path

    def __post_init__(self):
        path = Path(self.path).expanduser().resolve()
        relative = Path(self.relative_path)
        if relative.is_absolute() or ".." in relative.parts or not relative.name:
            raise ValueError("输出相对路径不能越过运行目录")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "relative_path", relative.with_suffix(".png"))


@dataclass(frozen=True)
class ProcessedImage:
    source: InputImage
    result: Result | None
    source_sha256: str
    mask_path: Path | None = None
    candidate_path: Path | None = None
    metrics: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ProcessingError:
    path: Path
    error: str


@dataclass
class BatchResult:
    options: Options
    items: list[ProcessedImage] = field(default_factory=list)
    errors: list[ProcessingError] = field(default_factory=list)
    cancelled: bool = False


@dataclass
class SaveSummary:
    output_dir: Path
    report_path: Path
    outputs: list[Path] = field(default_factory=list)
    cancelled: bool = False
    errors: list[ProcessingError] = field(default_factory=list)


class ScanCancelled(RuntimeError):
    """A directory scan stopped before publishing its input list."""


class ReplacementError(RuntimeError):
    """Replacement failed; report_path records backup and rollback state."""

    def __init__(self, message: str, report_path: Path, rollback_errors: list[str]):
        super().__init__(message)
        self.report_path = report_path
        self.rollback_errors = rollback_errors


class _Cancelled(Exception):
    pass


def _cancel(cancel: Cancel | None) -> None:
    if cancel is not None and cancel():
        raise _Cancelled()


def _natural(path: Path):
    parts = [(1, int(part)) if part.isdigit() else (0, part.casefold())
             for part in re.split(r"(\d+)", path.as_posix())]
    return parts, path.as_posix()


def _digest(path: Path) -> str:
    hasher = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def scan_inputs(paths: Iterable[Path | str], recursive: bool = False,
                exclude_paths: Iterable[Path | str] = (),
                cancel: Cancel | None = None) -> list[InputImage]:
    """Scan supported files in stable order, preserving each selected root.

    One selected directory has no extra prefix. With multiple roots, directories
    receive their root name. PNG name collisions receive a stable source-path
    hash suffix (including case-only names and different source extensions).
    Overlapping roots and symlink aliases are deduplicated by resolved path.
    """
    def check_cancel():
        if cancel is not None and cancel():
            raise ScanCancelled("已取消扫描")

    check_cancel()
    roots_set = set()
    for value in paths:
        check_cancel()
        roots_set.add(Path(value).expanduser().resolve())
    roots = sorted(roots_set, key=_natural)
    excluded = [Path(p).expanduser().resolve() for p in exclude_paths]
    for root in roots:
        check_cancel()
        if any(root.is_relative_to(exclusion) for exclusion in excluded):
            raise ValueError(f"输入路径位于排除目录中，请调整输入或输出位置：{root}")
        if not root.exists() or not (root.is_file() or root.is_dir()):
            raise ValueError(f"输入路径不存在或不是文件/目录：{root}")
        if root.is_file() and root.suffix.lower() not in SUPPORTED:
            raise ValueError(f"不支持的图片类型：{root.name}")
    found: dict[Path, Path] = {}
    for root in roots:
        check_cancel()
        if root.is_file():
            candidates = [root]
        else:
            candidates = []
            def raise_walk_error(error):
                raise error
            for parent, directories, files in os.walk(root, followlinks=False, onerror=raise_walk_error):
                check_cancel()
                parent_path = Path(parent)
                directories[:] = [name for name in directories if not name.startswith(".")
                                  and not (parent_path / name).is_symlink()
                                  and not any((parent_path / name).resolve().is_relative_to(p) for p in excluded)]
                for name in files:
                    check_cancel()
                    if not name.startswith("."):
                        candidates.append(parent_path / name)
                if not recursive:
                    break
        for path in sorted(candidates, key=_natural):
            check_cancel()
            if not path.is_file() or path.suffix.lower() not in SUPPORTED:
                continue
            resolved = path.resolve()
            if any(resolved.is_relative_to(p) for p in excluded):
                continue
            if resolved in found:
                continue
            if root.is_file():
                relative = Path(root.name)
            else:
                relative = path.relative_to(root)
                if len(roots) > 1:
                    relative = Path(root.name) / relative
            found[resolved] = relative.with_suffix(".png")
    counts: dict[str, int] = {}
    for relative in found.values():
        check_cancel()
        key = relative.as_posix().casefold()
        counts[key] = counts.get(key, 0) + 1
    used: set[str] = set()
    images = []
    for path, relative in sorted(found.items(), key=lambda pair: _natural(pair[0])):
        check_cancel()
        if counts[relative.as_posix().casefold()] > 1:
            token = sha256(str(path).encode("utf-8")).hexdigest()[:10]
            relative = relative.with_name(f"{relative.stem}-{token}.png")
        name, number = relative, 2
        while name.as_posix().casefold() in used:
            name = relative.with_name(f"{relative.stem}-{number}.png")
            number += 1
        used.add(name.as_posix().casefold())
        images.append(InputImage(path, name))
    check_cancel()
    return images


def _texture(options: Options) -> None:
    if options.method != "texture":
        raise ValueError("桌面批处理仅支持 texture 方法")


def process_single(source: InputImage | Path | str, options: Options) -> ProcessedImage:
    """Compute one image and reject a source that changed during processing."""
    _texture(options)
    if not isinstance(source, InputImage):
        path = Path(source)
        source = InputImage(path, Path(path.name))
    before = _digest(source.path)
    result = process_image(source.path, options)
    if _digest(source.path) != before:
        raise ValueError(f"处理过程中原图发生变化，请重新处理：{source.path}")
    return ProcessedImage(source, result, before, metrics=dict(result.metrics))


def process_batch(inputs: Iterable[InputImage], options: Options,
                  cancel: Cancel | None = None, progress: Progress | None = None,
                  cache_dir: Path | str | None = None,
                  processed: Callable[[ProcessedImage], None] | None = None,
                  max_workers: int | None = None) -> BatchResult:
    """Compute in the caller's worker; image errors do not stop a batch.

    cache_dir receives a fresh child directory on every call, so reprocessing
    with different parameters cannot overwrite an older result still in use.
    Cached desktop batches use at most two image threads; expensive native
    sparse solves release the GIL. Only that many jobs are submitted at once.
    Results are cached inside each job, and callbacks run in this caller's
    thread. Cancellation waits for the current native solves to finish without
    publishing their late results. In-memory batches stay sequential by default.
    An explicit max_workers (1 to 4) permits controlled comparisons.
    """
    _texture(options)
    sources = list(inputs)
    batch = BatchResult(options)
    if max_workers is not None and (isinstance(max_workers, bool)
                                    or not isinstance(max_workers, int)
                                    or not 1 <= max_workers <= 4):
        raise ValueError("并行处理数量必须是 1–4 之间的整数")
    cache = Path(cache_dir).expanduser().resolve() if cache_dir is not None else None
    if cache is not None:
        if any(source.path.is_relative_to(cache) for source in sources):
            raise ValueError("缓存目录不能是原图所在目录或其上级目录")
        cache.mkdir(parents=True, exist_ok=True)
        cache = Path(tempfile.mkdtemp(prefix="batch-", dir=cache))
    workers = max_workers if max_workers is not None else (
        min(2, max(1, (os.cpu_count() or 1) // 2))
        if cache is not None and options.texture_strength != 0 else 1)
    workers = min(workers, max(1, len(sources)))
    if workers > 1:
        return _process_parallel(sources, options, cache, workers, cancel, progress, processed)
    for index, source in enumerate(sources, 1):
        try:
            _cancel(cancel)
        except _Cancelled:
            batch.cancelled = True
            break
        try:
            item = process_single(source, options)
            _cancel(cancel)
            if cache is not None:
                item = _cache_item(item, cache, index)
            _cancel(cancel)
        except _Cancelled:
            batch.cancelled = True
            break
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            batch.errors.append(ProcessingError(source.path, str(error)))
        else:
            batch.items.append(item)
            if processed is not None:
                processed(item)
        if progress is not None:
            progress(index, len(sources), source.path)
    return batch


def _process_parallel(sources: list[InputImage], options: Options,
                      cache: Path | None, workers: int,
                      cancel: Cancel | None, progress: Progress | None,
                      processed: Callable[[ProcessedImage], None] | None) -> BatchResult:
    batch = BatchResult(options)
    items, errors = {}, {}
    submitted = 0
    completed = 0
    pending = {}

    def calculate(index, source):
        _cancel(cancel)
        item = process_single(source, options)
        _cancel(cancel)
        if cache is not None:
            item = _cache_item(item, cache, index + 1)
        _cancel(cancel)
        return item

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="glyph-texture")
    try:
        while submitted < len(sources) or pending:
            _cancel(cancel)
            # No executor.map or unbounded queue: sparse factorization needs
            # much more memory than the decoded glyph, even for a small PNG.
            while submitted < len(sources) and len(pending) < workers:
                _cancel(cancel)
                source = sources[submitted]
                future = pool.submit(calculate, submitted, source)
                pending[future] = (submitted, source)
                submitted += 1
            ready, _ = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
            for future in sorted(ready, key=lambda value: pending[value][0]):
                _cancel(cancel)
                index, source = pending.pop(future)
                try:
                    item = future.result()
                except (OSError, ValueError, Image.DecompressionBombError) as error:
                    errors[index] = ProcessingError(source.path, str(error))
                else:
                    items[index] = item
                    if processed is not None:
                        processed(item)
                completed += 1
                if progress is not None:
                    progress(completed, len(sources), source.path)
    except _Cancelled:
        batch.cancelled = True
    finally:
        for future in pending:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
    batch.items = [items[index] for index in sorted(items)]
    batch.errors = [errors[index] for index in sorted(errors)]
    return batch


def load_mask(item: ProcessedImage) -> np.ndarray:
    """Load only one binary mask, from memory or the worker's session cache."""
    if item.result is not None:
        mask = np.asarray(item.result.mask)
    elif item.mask_path is not None:
        with Image.open(item.mask_path) as image:
            mask = np.asarray(image.convert("L")).copy()
    else:
        raise ValueError("处理结果缺少掩膜")
    metrics = item.metrics or (item.result.metrics if item.result is not None else {})
    expected = (metrics.get("height"), metrics.get("width"))
    if mask.shape != expected or mask.dtype != np.uint8 or not np.isin(mask, [0, 255]).all():
        raise ValueError(f"掩膜必须与原图同尺寸且仅含 0/255：{item.source.path}")
    return mask


def load_result(item: ProcessedImage, foreground: str = "light") -> Result:
    """Load an individual preview without retaining the rest of the batch."""
    _check_source(item)
    if item.result is not None:
        return item.result
    if item.candidate_path is None:
        raise ValueError("处理结果缺少原始候选图")
    original = load_image(item.source.path, foreground)
    with Image.open(item.candidate_path) as image:
        candidate = np.asarray(image.convert("L")).copy()
    mask = load_mask(item)
    if candidate.shape != mask.shape:
        raise ValueError("缓存候选图尺寸不正确")
    return Result(original, candidate, mask, dict(item.metrics))


def _png_bytes(mask: np.ndarray) -> bytes:
    buffer = BytesIO()
    Image.fromarray(mask).save(buffer, format="PNG")
    return buffer.getvalue()


def _mask_bytes(item: ProcessedImage) -> bytes:
    return _png_bytes(load_mask(item))


def _cache_item(item: ProcessedImage, cache: Path, index: int) -> ProcessedImage:
    token = sha256(str(item.source.path).encode("utf-8")).hexdigest()[:12]
    directory = cache / f"{index:06d}-{token}"
    directory.mkdir(exist_ok=False)
    mask_path, candidate_path = directory / "mask.png", directory / "candidate.png"
    _atomic_write(mask_path, _mask_bytes(item), exclusive=True)
    _atomic_write(candidate_path, _png_bytes(item.result.candidate), exclusive=True)
    _report(directory / "metrics.json", item.metrics)
    return ProcessedImage(item.source, None, item.source_sha256,
                          mask_path, candidate_path, dict(item.metrics))


def _atomic_write(path: Path, data: bytes, exclusive: bool = False) -> None:
    """Publish only complete bytes; replacement stays on the same filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".hanzi-mask-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _report(path: Path, data: dict) -> None:
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))


def _new_directory(path: Path) -> Path:
    directory = path.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def _base_report(batch: BatchResult, operation: str) -> dict:
    return {
        "schema_version": 1,
        "tool_version": __version__,
        "operation": operation,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "parameters": batch.options.to_dict(),
        "needs_visual_review": True,
        "processing_cancelled": batch.cancelled,
        "processing_errors": [{"source": str(e.path), "error": e.error} for e in batch.errors],
        "images": [],
    }


def _validate_items(batch: BatchResult) -> None:
    _texture(batch.options)
    if not batch.items:
        raise ValueError("没有可保存的处理结果")
    sources = [item.source.path for item in batch.items]
    outputs = [item.source.relative_path.as_posix().casefold() for item in batch.items]
    if len(set(sources)) != len(sources) or len(set(outputs)) != len(outputs):
        raise ValueError("批次中存在重复原图或输出文件名冲突")


def export_batch(batch: BatchResult, output_dir: Path | str,
                 cancel: Cancel | None = None, progress: Progress | None = None) -> SaveSummary:
    """Export original-size binary PNGs and a JSON report in a new directory.

    Cancellation leaves completed exports and reports the partial run. Originals
    are never written. Errors are recorded per image so good results remain usable.
    """
    _validate_items(batch)
    requested = Path(output_dir).expanduser().resolve()
    if any(item.source.path.is_relative_to(requested) for item in batch.items):
        raise ValueError("输出目录不能是原图所在目录或其上级目录")
    directory = _new_directory(requested)
    summary = SaveSummary(directory, directory / "report.json")
    report = _base_report(batch, "export")
    try:
        for index, item in enumerate(batch.items, 1):
            _cancel(cancel)
            relative = Path("images") / item.source.relative_path
            record = {"source": str(item.source.path), "source_sha256": item.source_sha256,
                      "output": relative.as_posix(), "metrics": item.metrics}
            try:
                data = _mask_bytes(item)
                target = directory / relative
                if not target.parent.resolve().is_relative_to(directory):
                    raise ValueError("输出子目录越过运行目录")
                _cancel(cancel)
                _atomic_write(target, data, exclusive=True)
                summary.outputs.append(target)
                record.update(status="exported", output_sha256=sha256(data).hexdigest())
            except _Cancelled:
                raise
            except (OSError, ValueError) as error:
                record.update(status="error", error=str(error))
                summary.errors.append(ProcessingError(item.source.path, str(error)))
            report["images"].append(record)
            if progress is not None:
                progress(index, len(batch.items), item.source.path)
        _cancel(cancel)
    except _Cancelled:
        summary.cancelled = True
    report["status"] = "cancelled" if summary.cancelled else "completed"
    report["summary"] = {"exported": len(summary.outputs),
                         "errors": len(batch.errors) + len(summary.errors),
                         "remaining": len(batch.items) - len(report["images"])}
    _report(summary.report_path, report)
    return summary


def _check_source(item: ProcessedImage) -> None:
    if _digest(item.source.path) != item.source_sha256:
        raise ValueError(f"原图已变化，请重新处理后再替换：{item.source.path}")


def _rollback(entries: list[dict], directory: Path) -> list[str]:
    errors = []
    for entry in reversed(entries):
        if entry["status"] != "replaced":
            continue
        try:
            source, backup = Path(entry["source"]), directory / entry["backup"]
            if _digest(source) != entry["output_sha256"]:
                raise ValueError("替换后文件被其他程序修改，已保留备份以供恢复")
            data = backup.read_bytes()
            if sha256(data).hexdigest() != entry["source_sha256"]:
                raise ValueError("备份校验失败")
            _atomic_write(source, data)
            entry["status"] = "rolled_back"
        except (OSError, ValueError) as error:
            entry["rollback_error"] = str(error)
            errors.append(f"{entry['source']}：{error}")
    return errors


def replace_originals(batch: BatchResult, backup_dir: Path | str,
                      cancel: Cancel | None = None, progress: Progress | None = None) -> SaveSummary:
    """Replace PNG originals, with full backups and rollback on cancellation/error.

    A fresh backup directory and report remain after every started operation.
    Source hashes must still match the processed bytes. Non-PNG formats are
    rejected, avoiding lossy or incorrectly named JPEG replacements.
    """
    _validate_items(batch)
    if batch.cancelled or batch.errors:
        raise ValueError("处理未完整完成，请先处理成功并选择要替换的图片")
    requested = Path(backup_dir).expanduser().resolve()
    if any(item.source.path.is_relative_to(requested) for item in batch.items):
        raise ValueError("备份目录不能是原图所在目录或其上级目录")
    preflight_cancelled = False
    for item in batch.items:
        if cancel is not None and cancel():
            preflight_cancelled = True
            break
        _check_source(item)
        with Image.open(item.source.path) as image:
            if item.source.path.suffix.lower() != ".png" or image.format != "PNG":
                raise ValueError(f"原位替换仅支持 PNG；其他格式请导出到新目录：{item.source.path.name}")
        _mask_bytes(item)
    directory = _new_directory(requested)
    summary = SaveSummary(directory, directory / "replacement.json")
    report = _base_report(batch, "replace")
    report["status"] = "backing_up"
    _report(summary.report_path, report)
    try:
        if preflight_cancelled:
            raise _Cancelled()
        # All backups must be durable before the first original is changed.
        for index, item in enumerate(batch.items, 1):
            _cancel(cancel)
            _check_source(item)
            data = item.source.path.read_bytes()
            if sha256(data).hexdigest() != item.source_sha256:
                raise ValueError(f"备份过程中原图发生变化：{item.source.path}")
            relative = Path("originals") / f"{index:06d}.png"
            _atomic_write(directory / relative, data, exclusive=True)
            output = _mask_bytes(item)
            report["images"].append({"source": str(item.source.path), "backup": relative.as_posix(),
                                      "source_sha256": item.source_sha256,
                                      "output_sha256": sha256(output).hexdigest(),
                                      "status": "backed_up", "metrics": item.metrics})
        report["status"] = "replacing"
        _report(summary.report_path, report)
        for index, (item, entry) in enumerate(zip(batch.items, report["images"]), 1):
            _cancel(cancel)
            _check_source(item)
            data = _mask_bytes(item)
            if sha256(data).hexdigest() != entry["output_sha256"]:
                raise ValueError("处理结果在保存过程中发生变化")
            _cancel(cancel)
            _atomic_write(item.source.path, data)
            entry["status"] = "replaced"
            summary.outputs.append(item.source.path)
            if progress is not None:
                progress(index, len(batch.items), item.source.path)
        _cancel(cancel)
        report["status"] = "completed"
        _report(summary.report_path, report)
    except Exception as error:
        rollback_errors = _rollback(report["images"], directory)
        summary.outputs.clear()
        summary.cancelled = isinstance(error, _Cancelled)
        report["status"] = "rollback_failed" if rollback_errors else ("cancelled" if summary.cancelled else "rolled_back")
        report["error"] = "用户取消" if summary.cancelled else str(error)
        report["rollback_errors"] = rollback_errors
        _report(summary.report_path, report)
        if not summary.cancelled or rollback_errors:
            raise ReplacementError(report["error"], summary.report_path, rollback_errors) from error
        return summary
    return summary


def restore_replacement(report_path: Path | str,
                        progress: Progress | None = None) -> SaveSummary:
    """Restore bytes from a replacement report after verifying every file.

    Refuses missing/corrupt backups and externally changed current images before
    touching any source. Already-restored images are accepted for a retry. There
    is no cancel mid-restore, ensuring a requested recovery runs to completion.
    """
    path = Path(report_path).expanduser().resolve()
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("operation") != "replace" or report.get("schema_version") != 1:
        raise ValueError("不是受支持的原位替换恢复清单")
    entries = report["images"]
    for entry in entries:
        relative = Path(entry["backup"])
        backup = path.parent / relative
        if relative.is_absolute() or ".." in relative.parts or not backup.resolve().is_relative_to(path.parent):
            raise ValueError("备份路径越过运行目录")
        if _digest(backup) != entry["source_sha256"]:
            raise ValueError(f"备份缺失或校验失败：{backup}")
        if _digest(Path(entry["source"])) not in {entry["output_sha256"], entry["source_sha256"]}:
            raise ValueError(f"当前图片已被修改，拒绝覆盖：{entry['source']}")
    summary = SaveSummary(path.parent, path)
    report["status"] = "restoring"
    _report(path, report)
    try:
        for index, entry in enumerate(entries, 1):
            source = Path(entry["source"])
            # Check again immediately before publishing, including after callbacks.
            if _digest(source) not in {entry["output_sha256"], entry["source_sha256"]}:
                raise ValueError(f"恢复过程中图片已被修改，拒绝覆盖：{source}")
            if _digest(source) != entry["source_sha256"]:
                backup_data = (path.parent / entry["backup"]).read_bytes()
                if sha256(backup_data).hexdigest() != entry["source_sha256"]:
                    raise ValueError(f"恢复过程中备份已变化：{source}")
                _atomic_write(source, backup_data)
            entry["status"] = "restored"
            summary.outputs.append(source)
            if progress is not None:
                progress(index, len(entries), source)
        report["status"] = "restored"
        _report(path, report)
    except Exception as error:
        report["status"] = "restore_failed"
        report["restore_error"] = str(error)
        report["restored_count"] = len(summary.outputs)
        _report(path, report)
        raise ReplacementError(f"恢复未完成：{error}；可修正问题后重试同一恢复清单", path, []) from error
    return summary
