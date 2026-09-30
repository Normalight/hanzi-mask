# 首轮 DocEnTr 预训练测试

2026-10-01，对只读目录 `../10-1dbe/` 的 194 张拓片进行测试。模型、下载地址与复现环境见 [运行说明](DOCENTR.md)。使用同一份官方 Base/8/H-DIBCO 2018 权重，前景反相、阈值 0.5，不增加形态学后处理。设备为本机 MPS；运行时 PyTorch 2.14.0、torchvision 0.29.0、vit-pytorch 0.24.3、einops 0.8.2。

## 覆盖与导出

| 输入方式 | 完成数 | 全黑输出数 | 白色像素低于 1% 的图片数 |
| --- | ---: | ---: | ---: |
| 原尺寸 256×256 切块 | 194 | 0 | 5 |
| 整字最长边 256、补白 | 194 | 1 | 9 |
| 整字最长边 128、补白至 256 | 194 | 2 | 6 |

三组共 582 张掩膜均为原尺寸、单通道 0/255 PNG，运行无逐图错误。批处理报告中的来源哈希在处理后检查仍一致。前两组一次运行约 81 秒，128 组约 26 秒；此计时不包含安装、下载和模型加载。统计数据仅揭示异常输出，不能解释为分割准确率；没有人工真值，也未计算真实 IoU/F1。

## 接入核对

对官方 `demo/degraded/2.png` 使用 `--foreground dark --mode tile`，与仓库自带的 `demo/cleaned/2__best-model_8_2018base_256_8.png` 做黑白方向统一后的比较。3,731,195 个像素中 372 个不同，一致率约 99.9900%。本适配器先合成灰度再阈值化，官方样例按颜色通道阈值化；另有设备/浮点实现差异，因此不要求逐像素完全相同。

这支持权重加载、颜色顺序、归一化和拼块过程正确；官方预测样例不是人工真值，该一致率不是拓片准确率。

## 视觉观察

重点查看编号 0、17、20、60、74、90、110、126、140、160、180、193 的五列对照（原图、传统 texture、切块、整字 256、整字 128）。

- 原尺寸切块和整字 256 在多张图片上会大量漏掉笔画；例如 110、193，结果只剩碎点。模型对输入尺度明显敏感。
- 整字 128 在这些已查看的样例上较好地保留了主体，17、60 周围的细碎斑点也少于另两种模型输入；90、110、193 的主体重新出现。
- 同色粗斑和粘连仍无法稳定区分。0 在两种整字方式中完全消失；20、74、126、160 等仍保留粗噪声或有轮廓合并。
- 缩放也会损失细节。128 方式可以继续作为候选与现有算法并列复核，尚不足以替换默认算法或直接形成真实字形数据集。

该结论只适用于这份预训练权重与本次输入设置，不能推及所有深度学习方法。确认更佳方法需要具有拓片覆盖的模型权重，以及独立、人工确认的像素级评估集。

## 本地生成物与复现命令

图片、权重和报告均在 Git 忽略目录内：

- `output/10-1dbe-docentr/`：切块与整字 256 两组掩膜、分数图、33 页预览及报告。
- `output/10-1dbe-docentr-fit128/`：整字 128 掩膜、分数图、33 页预览及报告。
- `output/docentr-comparison.png`：编号 17、60、110、193 的五列对照。
- `output/docentr-review-a.png`、`output/docentr-review-b.png`：12 张代表样例。
- `output/docentr-validation.json`：尺寸、二值输出、来源哈希及异常统计。
- `output/docentr-official-check/`：官方样例的推理与一致性核对。

以下命令要求相应输出目录尚不存在；重复测试请更换目录名：

```sh
.venv/bin/python -m hanzi_mask.docentr ../10-1dbe \
  -o output/10-1dbe-docentr \
  --baseline output/10-1dbe-texture-balanced --device mps
.venv/bin/python -m hanzi_mask.docentr ../10-1dbe \
  -o output/10-1dbe-docentr-fit128 --mode fit --fit-size 128 \
  --baseline output/10-1dbe-texture-balanced --device mps
.venv/bin/python -m hanzi_mask.docentr output/research/DocEnTR/demo/degraded/2.png \
  -o output/docentr-official-check --foreground dark --mode tile --device mps
```

无 MPS 的机器可使用 `--device auto` 或 `--device cpu`。不需要传统对照列时，省略 `--baseline`。
