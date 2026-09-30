# DocEnTr 免训练实验

使用 [DocEnTr 官方仓库](https://github.com/dali92002/DocEnTR) 的 Base 模型、8×8 token 和 H-DIBCO 2018 公开预训练权重；本地推理不训练、不上传图片。论文为 [DocEnTr: An End-to-End Document Image Enhancement Transformer](https://arxiv.org/abs/2201.10252)。它是退化文档增强模型，对拓片的效果需另行验证。

本地 194 张拓片的三组输入实验及观察见 [实验记录](DOCENTR_EXPERIMENT.md)。

上游代码采用 [CC BY-NC 4.0](https://github.com/dali92002/DocEnTR/blob/main/LICENSE)，含非商业限制。官方源码和权重单独存放于被 Git 忽略的目录，本项目适配器不包含模型实现。部署或分发时需要单独核对上游许可。

## 安装

在 `hanzi-mask` 目录运行，Python 3.11+：

```sh
.venv/bin/python -m pip install -r requirements-docentr.txt
git clone https://github.com/dali92002/DocEnTR output/research/DocEnTR
git -C output/research/DocEnTR checkout 004159e10f69f2b33f5800eeba4175d6f93cb17a
mkdir -p models
.venv/bin/gdown 1CpvS9ahZolRz2sJ4PHobofOXVJBMWwD0 \
  -O models/docentr-base-2018.pt --no-cookies
```

权重链接来自官方 README 的 H-DIBCO 2018 / Base 行。此次下载文件的 SHA-256：

```text
d0294ad99fcde046c4298821183f7d38e712e3219728305fe0c52fec8ef75498
```

此哈希记录本次取得的文件，官方未提供签名。适配器使用 `weights_only=True` 加载状态字典并严格检查网络结构；不会在加载失败后改用不受限的 pickle。`--repo` 指向的 Python 模型代码会执行，应使用上述已核对的官方版本。

## 批量运行

```sh
.venv/bin/python -m hanzi_mask.docentr ../10-1dbe \
  -o output/docentr-test
```

默认对每张图运行两种方式：`tile` 保留像素尺度、按 256×256 切块；`fit` 把整字等比缩放至最长边 256，补白成一个输入块，再将输出分数图恢复原图尺寸。可用 `--mode tile` 或 `--mode fit` 单独运行，`--limit 12` 抽样。

已有传统算法输出时可增加 `--baseline output/10-1dbe-texture-balanced`，每张输入须有同名 PNG 对照。预览列为 SOURCE、TEXTURE、DOCENTR TILE、DOCENTR FIT。模型不会加载或使用传统算法掩膜。

整字占输入块的比例可以单独测试：

```sh
.venv/bin/python -m hanzi_mask.docentr ../10-1dbe \
  -o output/docentr-fit128 --mode fit --fit-size 128
```

其他参数：`--foreground dark` 用于浅底深字；`--device auto/cpu/mps/cuda` 选择设备；`--threshold 0.5` 设置前景分数阈值。默认自动选择 CUDA、MPS、CPU 中可用的设备。实验要求全新输出目录，避免覆盖已有对照。只扫描输入目录第一层，也支持单张图片；名称转换冲突会在导出前报错。

## 输出与适配

- `tile/`、`fit/`：原尺寸 0/255 黑底白字 PNG。
- `tile-scores/`、`fit-scores/`：反相重建灰度分数图，保存为 8 位，仅用于检查阈值；不是校准概率或字形正确率。
- `preview-*.jpg`：每页最多六张对照。
- `report.json`：设备、权重/模型源码哈希、参数、耗时、逐图状态和原图哈希。

拓片默认先反相成深字浅底。按官方训练约定采用 BGR 通道顺序与 ImageNet RGB 常量归一化。输出反归一化、裁剪到 0–1、合成为灰度，再反相并以 `>0.5` 提取前景。整字缩放先恢复分数图尺寸再阈值化。本实验不叠加开运算、面积过滤或传统掩膜，以便观察预训练模型本身的效果。

原尺寸切块可能产生块间差异；整字缩放改变模型看到的笔画尺度。两者均不能保证正确处理同色碎斑、粘连与残损。缺少人工真值时，前景比例、空白数量、与旧掩膜的一致率都不能作为真实准确率。默认 `app.py` 仍使用纹理方法。

## 验证

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m pip check
```

普通测试使用可控的预测函数检查切块边界、缩放补白、极小图片、颜色和前景方向、导出尺寸、来源保护、重复运行保护及损坏图片处理。它们不检验真实模型分割质量。实际推理需要下载资源后查看输出对照图。
