# 项目上下文

本项目是独立 Git 仓库，提供拓片单字的字形掩膜提取和黑底白字 PNG 导出。入口为 `app.py`，业务代码位于 `hanzi_mask/`。运行环境为 Python 3.11+；不依赖相邻项目源码、模型、认证服务或更新包。

默认 `texture` 方法先做 RTV 风格的灰度纹理/结构分离，再利用笔画尺度、区域面积和空间邻近关系生成掩膜；`--method morphology` 保留原有几何筛选流程。平滑依赖 SciPy 稀疏求解，工作图最长边限制为 512 像素，导出保持原尺寸。实现位于 `hanzi_mask/texture.py`，方法来源见 `docs/METHODS.md`。

输出是需要视觉复核的候选字形；粘连斑点、残损笔画与粗纹理可能需要调参或外部人工掩膜。平滑可能填充暗区，空间筛选可能误删点画；`--keep-isolated` 关闭空间筛选。对照图红色表示相对原始明暗候选删除的区域，青色表示增加的区域。不要将连通区域数量下降当作分割准确率，也不要将自动处理描述为已恢复真实字形。

`../10-1dbe/` 是本地测试图片，原图只读。虚拟环境、输出图片、对照图和报告均为生成物，不纳入 Git。正常导出不覆盖已有文件。

开发验证：

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
python app.py ../10-1dbe -o output/10-1dbe --preview
```

可选免训练实验入口为 `python -m hanzi_mask.docentr`，依赖 `requirements-docentr.txt`，安装和运行见 `docs/DOCENTR.md`。它加载独立下载的 DocEnTr 官方 `models/binae.py` 和 Base/8/2018 权重，采用严格匹配与 `torch.load(weights_only=True)`；输出目录必须是新目录。`models/` 与 `output/research/` 均为忽略的资源目录。上游采用 CC BY-NC 4.0，源码和权重不随本项目分发。实验提供原尺寸切块和整字等比缩放，导出恢复原尺寸，分数图不是校准概率。PyTorch 延迟导入，核心命令行及普通测试不依赖模型包。Mac 的自动设备选择优先 MPS，可明确使用 `--device cpu`。

配对监督实验入口为 `python -m hanzi_mask.supervised`，依赖 `requirements-training.txt`。`glyph_unet.py` 提供小型 U-Net，`supervised_data.py` 负责清单校验与合成退化。训练必须显式提供 image/mask 配对和独立 test 清单；原图内容重复或声明的来源 group 跨划分会拒绝运行。测试图不得用于生成训练背景、掩膜或选择检查点。合成验证使用训练字形的新噪声，只用于选择检查点，其 Dice 不代表真实测试准确率。输出、掩膜清单与权重放在 `output/`，不纳入 Git。`--checkpoint` 可只加载权重导出；运行说明见 `docs/SUPERVISED.md`。Torch 模型测试在依赖不可用时跳过，数据及核心测试仍运行。

`--augmentation complex` 使用 `degradation.py` 的多尺度碎斑、条纹、边缘污损和裂纹，形变标签采用软边缘覆盖度；默认 `simple` 保留首轮训练方式。`python -m hanzi_mask.synthesize` 可将训练条目导出为成对合成数据，绝不使用 test 作为背景或模板。多字清单可标记 `character` 和 `unseen_character`，后者不得与训练字符重叠。`--edge-sigma 0.7` 额外导出 `*-smooth` 二值掩膜和 `*-antialiased` 灰度展示图，原始输出继续保存在 train/test；抗锯齿图不可当作 0/255 掩膜。详见 `docs/MULTIGLYPH.md`。

修改算法后检查代表性拓片对照图，并如实区分合成图测试、真实图片批处理和人工视觉验收。测试图片不得作为已验证语义正确的标注。提交说明遵循工作区根 `AGENTS.md`。
