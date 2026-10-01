# 干净字形配对训练实验

以干净图及其黑底白字掩膜作为训练对，合成同色斑点、裂纹、亮度变化、模糊和形变，训练小型 U-Net 输出前景分数。实现使用 PyTorch 原语，架构参考 [U-Net 原始研究](https://lmb.informatik.uni-freiburg.de/people/ronneber/u-net/)，不加载 DocEnTr 参数。默认图像长边为 192，等比缩放后居中补黑；预测先裁去补边、恢复原尺寸，再以 0.5 阈值导出纯 0/255 PNG。

## 数据清单

掩膜需要与按 EXIF 校正后的原图同尺寸、单通道且像素只有 0 和 255。先用干净图片生成候选，再检查细笔画、独立点画、粘连杂点和边缘；自动阈值结果不能直接视为专家真值。

清单使用 JSON，路径相对于清单文件，也可以写绝对路径：

```json
{
  "character": "安",
  "train": [
    {"image": "clean/a.png", "mask": "masks/a.png", "group": "source-page-a"}
  ],
  "test": [
    {"image": "complex/b.png", "group": "source-page-b"}
  ]
}
```

`train` 与 `test` 均必须非空。工具校验原图 SHA-256、掩膜尺寸/二值性和输出名称。同一原图的缩放版本哈希可能不同，因此应将同一来源页或重复切片填入相同 `group`；该组不能同时出现在训练和测试。`group` 无法由任意文件名可靠推断，准备清单时必须核对。测试图片完全不进入增强生成器。

## 运行

```sh
.venv/bin/python -m pip install -r requirements-training.txt
.venv/bin/python -m hanzi_mask.supervised output/an-character/manifest.json \
  -o output/an-character/new-run --steps 1000 --batch-size 8 --size 192 --device auto
```

设备支持 `auto/cpu/mps/cuda`。默认种子 42；固定随机源有利于重现，但不同硬件、PyTorch 版本不保证逐位一致。输出目录必须尚不存在。核心 `app.py` 不受影响。

默认每 100 步在固定的 32 张合成样例上计算 Dice，保存分数最高的 `best.pt`。这些合成样例来自训练字形的新噪声；它们检验噪声扰动下的拟合，不能作为未见真实字形的泛化成绩。损失为 BCE 与 soft Dice 之和，优化器 AdamW，余弦学习率下降。

重新载入已训练权重，仅做预测：

```sh
.venv/bin/python -m hanzi_mask.supervised output/an-character/manifest.json \
  -o output/an-character/reloaded \
  --checkpoint output/an-character/run-1000/best.pt --device auto
```

保留训练清单与掩膜以便核对来源。检查点以 `weights_only=True` 读取，网络状态严格匹配；重载时校验清单原图/掩膜哈希与权重训练记录一致，并拒绝将权重见过的原图或来源组标成测试。

输出包括 `train/` 和 `test/` 掩膜、对应 `*-scores/` 分数图、每页最多六张的 `preview-*.png`，以及记录来源哈希、设备、选择步骤等信息的 `report.json`。训练运行另有 `best.pt`、`history.json` 和 `synthetic-examples.png`。预测分数未经概率校准，输出均需人工复核。

## “安”字首轮实验：2026-10-01

来源是工作区的 `output/字形图片高置信归并-20261001-1322/归类结果/按字/安/`，共 38 张 PNG。

- 训练 8 张：来源页 `16-d01f`、`55-9f82`、`20-a59e`（crop 55）、`43-cc65`、`51-a7d6`、`52-4dd0`、`59-375c`、`95-89db`。
- 排除 5 张：4 张对应训练切片的缩放版本，以及 `20-a59e` 的另一张同页字形。
- 测试 25 张：其余来源页图片，包含 `17-44ad`、`35-9588`、`69-fe23` 等复杂背景，也包含细笔画、低分辨率和较干净的样例。25 张并非全部重噪声。

训练掩膜使用 3×3 中值滤波、Otsu 阈值和最小区域 `max(5, 总像素数×0.0007)` 筛选。逐图查看原图/掩膜/叠加图后，额外移除 `55-9f82` 的三个边缘杂点区域和 `43-cc65` 的一个孤立区域；记录保存在清单中。这些是经过助手视觉检查的实验标签，尚未经专家逐像素确认。

MPS 设备训练 1000 步，每批 8 张，共 8000 个随机增强输入。选择依据只来自合成验证，最佳步骤为 900。合成验证 Dice 约 0.9883；此数值**不是 25 张真实测试图的准确率**。训练循环约 110 秒，不含后续对照生成；完整计时见报告。

保存的权重约 7.9 MB。重载后的 33 张训练/测试掩膜与首次导出逐像素一致，尺寸和 0/255 像素检查通过，原图及训练掩膜的哈希保持不变；检查明细保存在 `output/an-character/validation.json`。

视觉检查可见，部分图片保留了传统 texture 容易删掉的细笔画；`35-9588` 的周围碎斑较免训练 DocEnTr 结果减少。并非所有复杂图都改善：`17-44ad` 左侧粗纹理仍被当成前景，`69-fe23` 等仍存在细节丢失，部分字形边缘有锯齿。没有复杂图人工真值，本轮未计算真实 Dice/IoU，也不据此宣布通用模型优于其他方法。

本地资源均为 Git 忽略项：

| 路径 | 内容 |
| --- | --- |
| `output/an-character/manifest.json` | 原图、掩膜、来源组、训练/测试/排除划分 |
| `output/an-character/masks/` | 8 张训练掩膜 |
| `output/an-character/mask-review.png` | 原图、掩膜、绿色叠加对照 |
| `output/an-character/run-1000/best.pt` | 已训练权重 |
| `output/an-character/run-1000/test/` | 25 张独立测试输出 |
| `output/an-character/checkpoint-check/` | 保存后重载权重的复现检查及分页对照 |
| `output/an-character/complex-comparison.png` | 四张复杂图：原图、texture、DocEnTr 128、训练后 U-Net |
| `output/an-character/detail-comparison.png` | 四张细笔画/低分辨率样例对照 |

如继续改进，应增加干净字形来源和人工校正的复杂配对数据，并重新保留未用于调参的测试集。单字样本训练不能自动推广为所有汉字的准确分割器。
