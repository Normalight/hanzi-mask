# 拓片字形掩膜工具

从已切好的单字拓片中提取候选字形，批量导出纯黑背景、纯白前景的 PNG。保留原图尺寸和文件名主体，原图只读。当前版本提供命令行测试，不需要模型或远程服务。

斑点与笔画可能同色。本工具利用笔画宽度和连通区域面积筛除纹理碎斑，输出仍需视觉复核。粗斑、粘连和残损笔画可能处理错误；增大去噪强度也可能删除真实细笔画。当前算法没有识别字符含义，不能保证得到准确的真实字形。

## 快速开始

Python 3.11 或以上。在本项目目录运行：

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py ../10-1dbe -o output/10-1dbe-balanced --preview
```

Windows 下使用 `py -3.11 -m venv .venv` 创建环境，将后续 `.venv/bin/python` 替换为 `.venv\Scripts\python.exe`。

输出文件夹包含：

- 与输入对应的 `.png`：8 位灰度图，像素仅为 `0` 和 `255`。
- `preview-<运行编号>-<页码>.jpg`：原图、字形候选、变化区域三栏对照，每页最多 12 张。红色表示从明暗候选中删除的区域，青色表示人工掩膜增加的区域。
- `report-<运行编号>.json`：参数、逐图处理状态、尺寸、区域数量、删除比例及复核提示。统计数据不是准确率或模型置信度。

已经存在的输出图片默认跳过。重新试参数时使用不同输出目录，或者明确传入 `--overwrite`。每次运行生成独立报告。图片格式支持 PNG、JPEG、BMP、TIFF 和 WebP，多帧图片使用第一帧。

## 测试与调参

先抽样处理前 20 张：

```sh
.venv/bin/python app.py ../10-1dbe -o output/sample --limit 20 --preview
```

保留更多细节，或尝试更强去噪：

```sh
.venv/bin/python app.py ../10-1dbe -o output/10-1dbe-gentle --profile gentle --preview
.venv/bin/python app.py ../10-1dbe -o output/10-1dbe-strong --profile strong --preview
```

| 参数 | 作用 |
| --- | --- |
| `--profile gentle/balanced/strong` | 依照图片尺寸估计宽度和面积筛选强度，默认 `balanced` |
| `--stroke-radius 4` | 最小笔画尺度的圆盘半径，单位像素；越大越容易删除细笔画；`0` 关闭宽度筛选 |
| `--min-area 300` | 删除面积小于 300 像素的独立区域；可能误删点画；`0` 保留全部区域 |
| `--median-size 1` | 关闭默认的 3×3 中值滤波，保留更细的原始信息 |
| `--restore-steps 1` | 在原候选范围内恢复一层边缘，可能重新带回粘连碎斑；默认 `0` |
| `--foreground dark` | 处理浅底深字，导出仍为黑底白字；拓片默认 `light` |
| `--threshold 130` | 固定明暗候选的分界值；`dark` 时按反相后的亮度计算；默认 Otsu 自动估计 |
| `--recursive` | 扫描子目录并保留相对路径，排除输出和人工掩膜目录 |

单图调参例子：

```sh
.venv/bin/python app.py ../10-1dbe/10-1dbe_crop_17.png \
  -o output/single-17 --stroke-radius 4 --min-area 500 --preview
```

所有通过筛选的独立区域均保留，适应部件不相连的汉字。如果同目录中 `字.png` 和 `字.jpg` 转换后都叫 `字.png`，工具会在导出前报错，需先重命名。

## 逐图人工掩膜

需要精确结果的图片，可以在图像编辑器中描出真实笔画，保存同尺寸的黑底白字 PNG。例如原图为 `10-1dbe_crop_74.png`，人工掩膜放在 `masks/10-1dbe_crop_74.png`：

```sh
.venv/bin/python app.py ../10-1dbe -o output/reviewed --mask-dir masks --preview
```

提供掩膜的图片直接使用人工掩膜，不再进行自动去噪。未提供掩膜的图片继续自动处理；错误尺寸会报错。掩膜按照灰度 `>127` 作为前景，透明区域按黑色背景处理。子目录中的人工掩膜也按输入的相对路径排列。

## 算法与适用范围

1. 按 EXIF 校正方向、处理透明背景并转换灰度。
2. 中值滤波抑制像素级细噪声，估计明暗分界，形成笔画和同色斑点共同构成的候选。
3. 使用圆盘开运算筛除过细结构，再按连通区域面积去掉小碎斑。
4. 可选有限步边缘恢复，并输出掩膜、变化对照及参数记录。

Otsu 的作用是形成明暗候选；开运算通过腐蚀与膨胀筛除小结构。这些操作的定义见 [OpenCV 阈值文档](https://docs.opencv.org/4.x/d7/d4d/tutorial_py_thresholding.html)和[形态学文档](https://docs.opencv.org/4.x/d9/d61/tutorial_py_morphological_ops.html)。

同色、同宽且与笔画粘连的斑点，缺少可以由当前几何规则区分的信息。对于需要大批量精确分离真实字形的任务，应先建立人工校对的像素级标注，再训练专门的笔画/背景分割模型，并用独立标注集评估笔画保留率和斑点误留情况。本项目的候选掩膜可作为人工标注的起点，不应直接当作真实标签。

## 开发

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

也可以安装项目并使用 `hanzi-mask` 命令：

```sh
.venv/bin/python -m pip install -e .
.venv/bin/hanzi-mask --help
```

退出码：`0` 表示处理完成（包含跳过已有输出），`1` 表示存在文件处理或写入失败，`2` 表示参数或路径错误。单图失败会记录在报告中，继续处理其他图片。

`hanzi_mask/processing.py` 负责图像处理，`hanzi_mask/cli.py` 负责路径校验、批处理及报告，`hanzi_mask/preview.py` 负责对照图。`tests/` 覆盖同色碎斑、粘连细枝、独立点画、透明背景、EXIF、人工掩膜和文件写入保护。合成测试验证处理规则，真实图片效果需逐图查看；尚未对真实数据评估分割准确率。
