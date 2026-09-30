# 拓片字形提取的方法依据

## 已核对的相关工作

资料核对日期：2026-10-01。下表区分论文提出的方法、公开实现条件及本项目的使用方式。

| 工作 | 与本任务的关系 | 使用方式 |
| --- | --- | --- |
| [Xu et al., Structure Extraction from Texture via Relative Total Variation, ACM TOG 2012](https://www.cse.cuhk.edu.hk/~leojia/projects/texturesep/) | 通过局部变化和方向一致性区分纹理与主体结构 | 借鉴其重加权平滑思路，提供本项目的灰度适配实现 |
| [Huang et al., Character Segmentation of Chinese Rubbing Images Using Total Variation Preprocess, 2015](https://www.scientific.net/AMM.734.590) | 直接研究拓片，采用 TV 预处理后结合形态学分割 | 已核对出版社公开摘要，作为处理路线依据；未复现付费全文中的实验 |
| [Shi et al., CharFormer, ACM MM 2022](https://github.com/daqians/CharFormer) | 将字形信息融入深度去噪网络，关注笔画结构保留 | 作者公开了训练、推理代码；本次未找到可直接用于当前拓片的预训练权重 |
| [Ruan et al., LWTformer, CVPR Findings 2026](https://github.com/INWLY/LWTformer) | 结合可学习小波与注意力恢复古文字图像，在甲骨文和简牍数据上评估 | 官方说明给出训练和自备权重推理流程，许可限定学术研究；当前项目未集成其代码或模型 |

这些工作支持“先提取结构，再形成字形掩膜”的方向。不同材料、退化方式和字形之间存在差异，论文中的性能不能直接当作这批石刻拓片的处理效果。

## 当前实现

`texture` 方法采用方向加权的二次平滑：局部梯度较强、较大尺度方向变化较弱的位置获得更强平滑，连贯的边缘获得较弱平滑。使用四次重加权和 SciPy 稀疏线性求解，灰度范围归一化为 `[0, 1]`。边界采用反射滤波和非周期差分，最长工作边为 512 像素。

该方法是面向本任务的 RTV 风格适配，包括求解器、灰度输入及后续掩膜筛选的工程选择；未复现原论文的完整基准测试。代码来源为本项目实现，研究依据为上述作者项目页。

随后在平滑结果上计算 Otsu 阈值，并向亮部的第 95 百分位施加偏置，降低中间亮度纹理进入掩膜的机会。手动指定 `--threshold` 时覆盖这一自动选择。

| profile | 平滑强度 | 自动阈值偏置比例 | 开运算半径/短边 | 最小区域面积/全图 |
| --- | ---: | ---: | ---: | ---: |
| gentle | 0.006 | 0.25 | 0.010 | 0.001 |
| balanced | 0.012 | 0.40 | 0.015 | 0.003 |
| strong | 0.020 | 0.50 | 0.020 | 0.005 |

面积不小于最大部件 8% 的区域作为主体部件；较小区域距任一主体部件超过短边的 8% 时被筛除。距离按区域实际像素计算，保留多个较大的分离部件。`--keep-isolated` 可关闭空间筛选，适用于分离较远的点画。这些比例是工程启发式参数。

## 效果复核

用同一批原图、相同 profile 分别运行：

```sh
.venv/bin/python app.py ../10-1dbe -o output/control --method morphology --preview
.venv/bin/python app.py ../10-1dbe -o output/texture --method texture --preview
```

重点检查：外围碎斑是否减少、细笔画和独立点画是否消失、原有空洞是否被错误填充、不同部件是否发生错误粘连。报告中的连通区域数量和删除比例仅描述输出变化，区域数量减少也可能来自错误合并。

当前没有经过人工确认的真实像素标注，因此不报告真实数据的 IoU、F1 或准确率。自动测试验证数值行为、边缘保留、参数传递和批处理保护；真实字形的正确性仍需对照原图复核。
