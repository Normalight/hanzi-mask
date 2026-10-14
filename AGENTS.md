# 项目上下文

本项目是独立 Git 仓库，桌面显示名为“字形换底”，提供拓片单字的字形掩膜提取、桌面复核、黑底白字 PNG 导出和备份替换。仓库与分发包名为 `hanzi-mask`，Python 包为 `hanzi_mask`，独立产品标识为 `hanzi-mask`。`python app.py` 默认打开 PySide6 客户端；带参数保留命令行行为，`python -m hanzi_mask` 始终为 CLI。桌面代码位于 `ui/`，业务代码位于 `hanzi_mask/`。运行环境为 Python 3.11+；不依赖相邻项目源码或模型。GUI 启动接入共享登录、授权守卫与原生更新；命令行入口保持本地行为，不加载共享模块。安装后的桌面入口为 `hanzi-mask-desktop`，命令行入口为 `hanzi-mask`。

`requirements.txt` 引用本仓库 `vendor/hanzi_auth-0.2.12-py3-none-any.whl` 与 `vendor/hanzi_update-0.1.9-py3-none-any.whl`；`pyproject.toml` 将共享包归入可选 `platform` extra，核心安装不要求平台依赖。使用平台 extra 前先安装本地 requirements，不引用相邻源码。`hanzi-auth/tools/sync_desktop.py` 同步 wheel、公共打包脚本与 Sparkle bridge 源到 `group-by-character`、`char-detection-main` 和本项目；升级时人工核对 requirements、platform extra 与 `vendor/manifest.json` 的版本引用，不自动重建存在未提交修改的共享源码。

`ui/platform_integration.py` 提供延迟导入的平台适配，独立产品标识 `PRODUCT = "hanzi-mask"`。`ui/window.py` 的 `main()` 依次调用 `start_platform_session`（取消登录直接退出）与 `attach_platform_services`（授权守卫与原生更新接入）。产品已在 `hanzi-update` 的 products.json 注册表登记，服务端、授权管理与公共打包脚本从注册表识别 `hanzi-mask`；v1.0.0 起发布签名安装包与稳定更新源（`hanzi-mask-stable`）。适配层不修改平台账号与授权。

平台锁定救援保存将已采用的缓存结果固定导出为副本，不能沿用普通保存控件的原位替换模式。锁定时协作取消计算并等待任务结束，保留结果缓存与 Undo；更新时，有采用结果则先后台导出，仅本次导出成功且采用范围仍一致时允许继续退出。原生更新 `preparing` 阶段的试探关闭不得清理缓存与 Undo，实际退出继续遵守任务结束后清理的生命周期。修改启动接入时覆盖 GUI 启动接线（登录取消不创建窗口）、命令行不加载共享模块、锁定取消与救援导出、更新准备关闭等回归；本地接入不等于平台或原生更新已验收。

默认 `texture` 方法先做 RTV 风格的灰度纹理/结构分离，再利用笔画尺度、区域面积和空间邻近关系生成掩膜；`--method morphology` 保留原有几何筛选流程。平滑依赖 SciPy 稀疏求解，工作图最长边限制为 512 像素，导出保持原尺寸。实现位于 `hanzi_mask/texture.py`，方法来源见 `docs/METHODS.md`。

texture 在颜色证据充足时，对中性石面上色相一致的着色少数主体使用颜色差异前景通道，再做相同纹理平滑和筛选；灰度图继续原亮度流程。颜色证据记录于 `foreground_signal` 与 `color_evidence`，不能视为字符识别或分割置信度。同色污渍与独立点画仍需复核。

桌面默认启用 `Options.stroke_recovery=True`，CLI 默认关闭。`hanzi_mask/stroke_recovery.py` 在 RTV 自动阈值后、几何筛选前，用偏亮阈值生成的种子重建连贯的较弱结构。许可域由较低结构阈值和矩形灰度开运算估计的局部背景对比共同约束；原始候选仅支持有限的弱阈值放宽，不直接回加候选噪声。每区域至少 3 个种子，扩张最多 8 倍，超过 4 倍时要求原候选覆盖至少 60%；区域和总前景不超过画布 60%、画幅周长覆盖不超过 35%。拒绝新增时保留原亮种子；此保证仅针对重建步骤，后续宽度/面积筛选仍可能删除暗细桥。重建不保证语义或拓扑，应复核字内留白与粘连。原图超过 4,000,000 像素时跳过，不增加 RTV 求解次数；人工掩膜、显式阈值、morphology 与低对比图片绕过该步骤。

结构重建指标分为 `*_before_geometry` 和 `stroke_recovery_final_surviving_added_pixels`，后者仅表示自动处理结束时保留的本步骤新增像素，不代表真实笔画修复成功或人工矫正后的存留量。人工清理保留上游提取记录，桌面人工清理后的复核不再依据该初始存留量生成补入提示。

桌面 texture 默认启用 `Options.edge_refine=True`，示例准备脚本使用相同设置；CLI 默认关闭。`hanzi_mask/edge_refinement.py` 使用中值滤波前的原尺寸亮度或颜色差异信号，只在已接受掩膜边界内外一像素窄带内修正明确偏离原图的像素。局部证据不足、缺少内侧锚点的细笔画或画布边缘维持原掩膜；逐点保护 8 连通前景与 4 连通背景，并复核组件和孔洞数。不能补回已删除的部件或断笔，也不能保证消除原图中的边缘纹理。人工掩膜和桌面人工清理结果不再经过自动细化。输出尺寸与 0/255 格式保持不变，实际改动及跳过原因记录在 `edge_*` 指标。

边缘细化核对前景/背景双向位移，超过一原图像素时整体保留原掩膜；原图超过 4,000,000 像素或提案超过 100,000 个时跳过新增细化步骤，记录 `unchanged_resource_limit`。预算判断和有界校验避免为大整页图新增无上限工作数组；不通过缩小图片后再放大修改轮廓。纹理处理原有行为继续保留。

桌面和示例默认启用 `Options.tip_recovery=True`，CLI 默认关闭。`hanzi_mask/tip_recovery.py` 在所有几何筛选及边缘细化之后，使用中值滤波前的原尺寸亮度或颜色信号补回短小、与已接受笔画相连且有原图支持的末端候选；此后不再裁剪。该步骤只增不减，限制源支持尾部长度、形状和面积，保护 8 连通前景和 4 连通背景的组件与孔洞。不能补回完全丢失的独立部件，也可能拒绝方向不明确或较长的真实收笔；同色纹理仍需视觉复核。人工掩膜、显式阈值、morphology、低对比图片绕过；实际新增及跳过原因记录在 `tip_*` 指标。`tip_added_pixels` 表示最近一次自动提取时新增的像素，不代表人工清理后的当前存留量；人工清理保留上游记录而不触发自动补回。

笔尖距离上限为 `max(2, min(8, 2 * stroke_radius + 1))` 个原图像素，8 邻域生长最多同样轮数；先核对完整原图亮域超出既有边界一像素后的源尾部，超过距离、区域尺寸、生长轮数或触及画布边界时拒绝整块，避免将长细附枝截短后回加。新增需延续局部笔画轴向或有明确渐收证据，单区域面积最多 64 像素，总新增预算为原前景约 6%（最低 8 像素）。原图超过 4,000,000 像素、原图扩展候选超过 100,000 像素或近距离候选区域超过 256 个时跳过；区域循环仅操作固定小裁片，全图组件/孔洞检测只在最终有新增时执行。

输出是需要视觉复核的候选字形；粘连斑点、残损笔画与粗纹理可能需要调参或外部人工掩膜。平滑可能填充暗区，空间筛选可能误删点画；`--keep-isolated` 关闭空间筛选。对照图红色表示相对原始明暗候选删除的区域，青色表示增加的区域。不要将连通区域数量下降当作分割准确率，也不要将自动处理描述为已恢复真实字形。

`../10-1dbe/` 是本地测试图片，原图只读。虚拟环境、输出图片、对照图和报告均为生成物，不纳入 Git。正常导出不覆盖已有文件。桌面默认“保存到原位置”，仅支持真实 PNG；保存前由用户确认，先完整备份全部采用项，再逐图原子写入，取消或异常尝试回滚。“导出副本”仍可选，其他输入格式必须导出为 PNG 副本。每次保存创建独立运行目录，原位置保存的备份为 `originals/`、恢复清单为 `replacement.json`，副本结果为 `images/`，报告为 `report.json`；保存目录不能等于输入目录或包含输入路径，输入子目录中的保存目录会在扫描时排除。`hanzi_mask.batch_service.restore_replacement()` 保留恢复服务，依据清单校验备份和当前文件的哈希，不覆盖外部修改。替换/恢复后清除旧处理记录，避免预览、勾选状态与磁盘内容不一致。替换测试仅使用临时图片副本。

`ui/task_worker.py` 使用 QThread 扫描、处理和保存，主线程只负责控件和画布。计算结果放在会话缓存，仅加载当前图；关闭窗口时协作取消，保留线程与缓存直到任务结束，不使用强制终止。扫描取消通过持久结果标记回传，不能在已结束的线程上读取中断状态。桌面强度提供同步的滑块与 `0–100%` 数值输入，支持一位小数；“恢复默认强度”采用 balanced 推荐值。默认勾选“保留独立点画”，其余算法设置采用 balanced、自动阈值/区域尺度和内部默认值。点画选项仅关闭空间孤岛筛除，仍执行宽度和面积筛选，不能补回其他阶段删除的笔画。预览栏的当前图采用/取消采用动作与表格勾选双向同步，保存仅包含采用项。预览始终适配，不暴露缩放参数。`hanzi_mask/batch_service.py` 提供无 Qt 的任务接口，桌面仅使用 texture。修改异步生命周期、保存或恢复时覆盖 Qt 和服务回归。

参数应用范围默认“当前图片”，按路径保存草稿，实际切图恢复草稿或实际结果参数；内部预览刷新保留控件参数和未提交输入。“全部图片”范围维护统一设置，仅在用户点击处理按钮时应用；此范围下批量处理使用统一参数，当前图片范围下批量处理沿用逐图草稿或实际参数。修改参数保留结果、采用选择、复核状态和 Undo；预览与保存使用已提交结果。处理启动保存目标行快照，成功才提交该图的新结果并清除旧 Undo；失败、取消或未提交目标恢复旧状态和历史，采用选择尊重处理期间的用户勾选。重扫保留路径仍在输入集中的记录、草稿与历史，重新绑定导出相对路径，并显式刷新预览。

图片处理任务运行时，参数区的强度、点画、应用范围和线程数保持可编辑，表格切图、筛选和采用保持可用。当前图自动调参采用 350ms 防抖，只保留最新待运行请求；新请求替换旧自动任务时协作取消，等待旧 QThread 的 `finished` 后串行启动最新任务，过期回调不提交。原生求解返回后才能完成取消。主任务及预览信号通过显式 Qt 排队连接到窗口 Slot，在主线程提交或清理。手动批量任务持续使用启动时冻结的参数，后续编辑草稿保留；若当前图片范围开启自动处理，批量结束后处理最新单图草稿。内部处理结果筛选临时保留正在输入或有待运行请求的当前行，避免自动切图改变参数所属图片；用户主动切图或筛选继续生效。实际切图、切换范围、关闭自动、显式取消、进入模态窗口或关闭清空待运行自动请求；扫描、导出、替换和恢复等任务继续锁定冲突操作。

强度输入关闭键盘即时提交，按回车或失去焦点后提交；键入时暂停旧防抖，避免旧参数任务打断尚未完成的输入。百分比输入与滑块均按 0.1% 精度同步，程序同步控件时屏蔽信号，保留默认推荐值的参数语义。滑块释放只完成已有请求的防抖，显式取消后的输入结束只提交草稿，不重启同次请求；新的调参动作可再次自动排队。撤销清理先清空待运行自动请求。显式任务启动先清空待运行自动请求，文件选择及所有模态窗口通过 `_show_dialog` 清空请求并暂停自动重算，防止焦点提交再次排队；暂停计数在 `finally` 中恢复。当前图按钮、表格、全部采用、Undo 与保存列表统一限制为非空结果可采用；重算或人工清理期间可调整已有结果的采用选择，导出、替换、恢复及关闭期间冻结。采用只决定保存范围，不改变掩膜或复核状态；取消采用不删除缓存结果。

`ProcessedImage.options` 保存实际处理参数，缓存、人工清理和 Undo 继续携带；`process_batch(..., per_image_options=...)` 可按输入路径覆盖默认参数。保存报告格式版本为 2，逐图 `parameters` 来自结果；只有完整且统一时顶层 `parameters` 为共同参数，否则为 `null`，通过 `parameter_mode` 和 `parameters_complete` 描述。`batch_context_parameters` 是任务默认值，不能代表所有图片实际参数。恢复入口兼容格式版本 1、2，继续严格校验路径、备份和哈希。

`hanzi_mask/manual_correction.py` 将人工清理保存为独立缓存版本，不修改原图、旧结果或输入数组；重新计算掩膜指标并记录人工矫正历史。桌面点击中间前景按 8 连通性选块，Ctrl/Command 多选，清理/撤销仅改变候选。每图保留最多 20 步 Undo；切图、调参和重扫保留该图历史，成功重新处理后清除对应图的旧历史。白底预览只反相显示，底层掩膜和保存始终为 0/255 黑底白字。适配预览和选区高亮使用显示插值，选择始终映射回原始二值像素；显示抗锯齿不写入缓存或导出。

点击中间结果色块使 `MaskCanvas` 获得键盘焦点，Backspace/Delete 在该画布有选区时触发清理；没有选区时不清理。删除键由画布局部事件处理，不注册全窗口删除快捷键。路径或强度等输入框有焦点时，两键只编辑文本，保留选区与已有结果。

桌面复核文案由 `ui/review_hints.py` 根据结构化指标和撤销可用状态生成，不透传 CLI 参数建议。只引导当前界面支持的操作：调节强度、保留独立点画、处理当前图片、选块清理、撤销清理、取消采用；清理不能补回缺失笔画。表格与桌面保存报告使用相同复核提示，人工清理及撤销后保留仍适用的提示。

桌面可选择处理线程数，`hanzi_mask/concurrency.py` 统一定义当前应用上限（4）与本机推荐值；界面根据 CPU 和内存显示最大可选值，默认使用推荐值，通常为 2。改变线程数保留已有结果和人工矫正，仅用于下一次处理。最多提交同样数量的在飞任务，回调仍由外层任务线程串行发出。`process_batch(..., max_workers=1)` 可用于串行对照；未显式传值的缓存调用默认最多两个线程，关闭纹理平滑时默认串行。取消停止提交新任务并等待当前求解结束，丢弃未提交的结果。

`tools/prepare_client_examples.py` 从工作区本地图片或 `--source` 复制抽样，生成 `input/`、`expected/`、对照图和哈希清单；默认最多 16 张、目录 `output/client-examples/`，包含可用的红字尖细收笔和明暗不均“朝”字各两个尺寸，使用桌面默认点画、结构重建、边缘与笔尖设置。要求新输出目录；expected 是当前算法候选，不能作为已校对标注。`python app.py --gui --examples` 直接打开默认示例。

开发验证：

```sh
python -m pip install -r requirements-dev.txt
QT_QPA_PLATFORM=offscreen python -m pytest -q
python app.py ../10-1dbe -o output/10-1dbe --preview
```

可选免训练实验入口为 `python -m hanzi_mask.docentr`，依赖 `requirements-docentr.txt`，安装和运行见 `docs/DOCENTR.md`。它加载独立下载的 DocEnTr 官方 `models/binae.py` 和 Base/8/2018 权重，采用严格匹配与 `torch.load(weights_only=True)`；输出目录必须是新目录。`models/` 与 `output/research/` 均为忽略的资源目录。上游采用 CC BY-NC 4.0，源码和权重不随本项目分发。实验提供原尺寸切块和整字等比缩放，导出恢复原尺寸，分数图不是校准概率。PyTorch 延迟导入，核心命令行及普通测试不依赖模型包。Mac 的自动设备选择优先 MPS，可明确使用 `--device cpu`。

配对监督实验入口为 `python -m hanzi_mask.supervised`，依赖 `requirements-training.txt`。`glyph_unet.py` 提供小型 U-Net，`supervised_data.py` 负责清单校验与合成退化。训练必须显式提供 image/mask 配对和独立 test 清单；原图内容重复或声明的来源 group 跨划分会拒绝运行。测试图不得用于生成训练背景、掩膜或选择检查点。合成验证使用训练字形的新噪声，只用于选择检查点，其 Dice 不代表真实测试准确率。输出、掩膜清单与权重放在 `output/`，不纳入 Git。`--checkpoint` 可只加载权重导出；运行说明见 `docs/SUPERVISED.md`。Torch 模型测试在依赖不可用时跳过，数据及核心测试仍运行。

`--augmentation complex` 使用 `degradation.py` 的多尺度碎斑、条纹、边缘污损和裂纹，形变标签采用软边缘覆盖度；默认 `simple` 保留首轮训练方式。`python -m hanzi_mask.synthesize` 可将训练条目导出为成对合成数据，绝不使用 test 作为背景或模板。多字清单可标记 `character` 和 `unseen_character`，后者不得与训练字符重叠。`--edge-sigma 0.7` 额外导出 `*-smooth` 二值掩膜和 `*-antialiased` 灰度展示图，原始输出继续保存在 train/test；抗锯齿图不可当作 0/255 掩膜。详见 `docs/MULTIGLYPH.md`。

修改算法后检查代表性拓片对照图，并如实区分合成图测试、真实图片批处理和人工视觉验收。测试图片不得作为已验证语义正确的标注。提交说明遵循工作区根 `AGENTS.md`。

使用生成式修复掩膜时，保留原始生成图、逐次提示词、最终选择和哈希记录，逐图检查与原图的位置及结构对应关系。尺寸相同不代表标签对齐；修复推断属于伪标签，不能声明为专家真值。按完整画布恢复原尺寸后生成 0/255 训练标签，另存高分辨率结果；测试图不参与生成。流程与配对训练对照见 `docs/IMAGEGEN_MASKS.md`。

## CI 与打包

ZIP 边界：Windows `shutil.make_archive` 默认允许 Zip64；Mac `ditto` 的传统 ZIP 大小字段可能溢出，源文件合计达到 4 GiB 或源条目达到 65,535 时拒绝打包。两平台压缩后核对常规源文件条目大小并执行 ZIP CRC 检查，通过后才生成流式 SHA-256 并上传。不要使用 `archive.read_bytes()` 计算大包摘要。边界测试用临时稀疏文件或降低 Python ZIP64_LIMIT，不触发客户端构建。

`.github/workflows/native-build.yml` 为正式发布构建：输入版本（需已提交 `RELEASE_NOTES_v<版本>.md`）与平台，安装 WinSparkle/Sparkle SDK、Cython 保护后产出 Windows 安装器与 Mac 更新 ZIP；`publish=false` 只上传 Artifact，签名发布由本机密钥或 CI Secrets 完成。更新签名私钥为本机钥匙串 `https://sparkle-project.org` 的 `hanzi-hanzi-mask-stable` 账户与仓库 Secret `HANZI_UPDATE_PRIVATE_KEY`（与 `packaging/native_update_public.json` 公钥配对），私钥不得进入仓库或文档。本地发布：下载三端 Artifact 后用 `packaging/native_appcast.py` 生成签名 feed，`packaging/native_publish.py --github-only` 发布 GitHub 版本与 stable appcast，GitCode 与平台登记由认证服务镜像器完成。`build_mac.sh [arm64|x86_64]` 构建 Mac 原生结构（Runtime.app + 外层产品 app），设置 `HANZI_UPDATE_FEED` 时由 `native_release.py` 生成签名更新包。

`.github/workflows/build.yml` 仅 `workflow_dispatch` 手动触发，可选全部或单一平台，用于快速产物验证；不得在仅配置 CI 的任务中触发构建。Python 3.11，Windows x64 使用 `windows-2022`，macOS arm64 使用 `macos-15`，macOS x86_64 使用 `macos-15-intel`。流程依次校验依赖/原生架构/vendored wheel 摘要、运行测试、Cython 保护、PyInstaller 打包、独立临时目录的成品检查并上传 ZIP 与 SHA-256。版本取 `pyproject.toml`，产物保留 14 天；无发布任务或发布凭据要求。Mac 仅 ad hoc 签名，Developer ID 与公证需后续单独配置。

依赖统一在 `packaging/requirements-build.txt`，平台 requirements 引用它。`packaging/build_desktop.py --check --target macos-arm64` 仅检查，无编译；`--target` 不带 `--check` 才实际构建。保护后的业务入口包含 `ui/window.py`；成品的 `--smoke-test` 检查本地窗口、图片编解码和 texture 求解，不启用认证或更新。不需要训练模型、邻仓源码、GitCode 或更新 SDK 原生发行资源。

`packaging/protection.py` 原样复制自 `hanzi-auth/tools/desktop_packaging/protection.py`，摘要 `5045e63416373304b40b7f74e3e69876e952a539e4e87abdac201e8bc0e733a3`。公共逻辑只在源头改，本项目尚未加入共享同步脚本，需显式复制并核验。其他新增打包脚本为本产品维护。源码默认输出仍在项目 `output/字形换底/`；冻结运行默认在用户文稿 `字形换底/`（缺文稿路径退回主目录），由 `ui/paths.py` 决定，不写应用资源目录。
