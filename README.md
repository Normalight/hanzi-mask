# 字形换底客户端与掩膜工具

从已切好的单字拓片中提取候选字形，批量导出纯黑背景、纯白前景的 PNG。桌面客户端提供三栏预览、逐图复核、导出副本和带备份的原位替换。使用本地 `texture`，无需模型或远程服务。命令行接口继续可用；可选的 [DocEnTr 免训练实验](docs/DOCENTR.md) 使用单独下载的预训练权重在本地推理。

斑点与笔画可能同色。默认的 `texture` 方法先做 RTV 风格的纹理与结构分离，再筛选笔画尺度、连通区域面积及空间邻近关系。输出仍需视觉复核：粗斑、粘连和残损笔画可能处理错误，较强平滑也可能合并细节或改变轮廓。算法没有识别字符含义，不能保证得到准确的真实字形。

中性石面上的红色等着色字，在颜色证据充足时先使用颜色差异提取前景，再运行 texture 与区域筛选，避免灰度亮度接近导致主体消失。普通灰度拓片继续使用原有亮度流程；同色污渍仍需复核。

方法调研、来源与设计取舍见 [研究说明](docs/METHODS.md)。

已有干净单字图片时，可制作配对掩膜并训练小型 U-Net；训练、划分约束与“安”字实验见 [监督训练说明](docs/SUPERVISED.md)。

跨字形训练、复杂纹理/掩膜成对生成和边缘平滑导出见 [多字形与纹理增强实验](docs/MULTIGLYPH.md)。

逐张用 imagegen 修复训练掩膜、检查原图对应关系并重训的流程见 [生成式修复标签实验](docs/IMAGEGEN_MASKS.md)。

## 快速开始

Python 3.11 或以上。在本项目目录运行：

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

Windows 下使用 `py -3.11 -m venv .venv` 创建环境，将后续 `.venv/bin/python` 替换为 `.venv\Scripts\python.exe`。

`requirements.txt` 使用本仓库 `vendor/` 中的认证与更新 wheel，便于独立分发；默认 CLI 和 GUI 只启动本地功能，不加载这些模块。通过 `pip install -e .` 安装项目时，平台依赖归入可选的 `platform` extra，核心安装继续可用。桌面和命令行均不引用相邻项目源码。

客户端操作：

1. 添加图片、文件夹，或直接拖入窗口；默认扫描子文件夹。
2. 先处理当前图片，查看原图、黑底白字和变化对照；预览始终适配窗口。
3. 选择参数的“应用范围”，默认“当前图片”，每张图独立记住参数；停止调节后自动处理当前图，也可关闭“自动处理当前图片”后手动应用。选择“全部图片”时调整统一参数，再点击“批量处理全部”应用到所有图片；在“当前图片”范围下批量处理则沿用各图保存的参数或草稿。强度可拖动滑块或输入 `0–100%`，支持一位小数，两者同步；输入后按回车或移开焦点确认。数值较小保留更多细节，较大加强去噪。“保留独立点画”默认勾选，保留通过宽度和面积筛选的远处区域，可能同时留下碎斑。点击“恢复默认强度”回到 `balanced` 推荐值；其他算法参数采用内部默认值。
4. 批量处理后筛选“待复核”，检查细笔画、独立点画和残留碎斑。点击中间结果的前景色块选中一个连通区域，画布随即获得键盘焦点；点击“清理选中色块”或按 Backspace/Delete 清除，可用 Ctrl/Command 多选。背景点击取消选择，“撤销清理”恢复当前图上一步，最多保留 20 步。矫正后的结果参与导出或替换；在预览栏采用或取消采用当前图片，按钮状态与表格“采用”勾选同步，保存只包含非空的采用项。
5. 默认“保存到原位置”，仅支持真实 PNG。确认后先备份全部采用项的原图，再保存黑底白字结果；取消或失败会尝试回滚。需要保留原图或处理其他格式时，可选择“导出副本”。

调参保留已有结果、采用选择、复核状态和人工清理记录，尚未应用的参数会显示提示，预览与保存继续使用已有结果。重新处理成功后替换对应图片的结果，并清除该图旧的撤销记录；取消或失败时保留原结果和历史。重新扫描保留仍在列表中的图片记录，并补入新图片。

图片处理中仍可调整强度、“保留独立点画”、应用范围和线程数，也可切图、筛选或调整采用。自动处理当前图片在停止调节 350ms 后仅保留最新一次重算请求；旧自动任务收到取消请求后，等待当前计算结束，再串行开始最新请求，过期结果不覆盖已提交结果。手动发起的批量处理持续使用启动时的参数；期间编辑的草稿保留，若“当前图片”范围开启自动处理，则在批量结束后处理最新的单图草稿。

内部预览刷新保留参数和正在输入的内容；处理结果更新不会因状态筛选切走正在输入或等待重算的当前图。实际切图、切换应用范围、关闭自动处理、点击取消、打开模态窗口或关闭客户端时，会清除待运行的自动请求。扫描、导出等任务仍锁定会与任务冲突的操作；保存期间冻结采用选择。

“处理线程”可选择同时计算的图片数量，界面显示本机推荐值和最大可选值，默认使用推荐值。当前应用上限为 4，再根据 CPU 和内存降低本机上限；通常推荐 2。最大可选值是应用的处理上限，更多线程不一定更快。调整线程数用于下一次处理，保留已有结果与人工矫正。

中间结果可通过右键菜单切换黑底/白底预览：黑底点击白色，白底点击黑色；保存始终使用黑底白字。色块按 8 邻域连通性选取，和笔画粘连的斑点会与笔画一起选中，清理前可查看高亮范围，误删后撤销。Backspace/Delete 仅在结果画布有焦点且有选区时清理候选；输入框有焦点时，两键只编辑文本。

对于同一笔画明暗不均的情况，客户端先从亮笔画种子出发，在有局部结构对比与连续性支持的区域内重建较暗的笔画候选，再执行宽度和面积筛选。新增区域有增长、占比和边界限制；相邻石面纹理仍可能被带入，暗细笔画也可能继续被筛除，需检查字内留白和粘连。

之后使用原图信号细化已保留的笔画边沿：仅调整边界内外一像素内、且原图证据明确的像素；该细化步骤保护连通部件与字内孔洞。证据不足时保留当前轮廓。适配预览使用抗锯齿显示，导出仍保持原图尺寸及纯黑白二值像素；人工清理后不会再自动改动轮廓。

客户端在自动处理末尾启用笔尖保护，使用中值滤波前的原图亮度或颜色信号，尝试补回与保留笔画相连的短小尖细收笔。新增必须有原图对比证据，通过距离、形状、面积及连通检查；不再经过后续开运算或边缘裁剪。长细附枝、成片外扩、组件桥连和字内孔洞变化会被拒绝，证据不足时保留当前结果。该步骤仍可能漏掉较长或方向不明确的收笔，补入区域需要对照原图复核。

为控制新增步骤的资源占用，超过 400 万像素时跳过结构重建、边缘细化和笔尖保护；边缘调整提案或笔尖原图扩展候选超过 10 万个时跳过对应步骤。笔尖保护最多检查 256 个近距离源区域。继续使用相应步骤前的候选，并在报告中记录跳过原因。

每次保存创建独立运行目录。默认原位置保存的备份位于 `originals/`，恢复清单为 `replacement.json`；选择导出副本时，结果位于 `images/`。保存报告为 `report.json`。保存目录可放在输入目录的子文件夹中，扫描时会排除它；不能选择输入目录本身或包含输入的上级目录。原位置保存仅支持真实 PNG，其他格式需导出副本。处理后原图有变化时会拒绝替换；恢复服务依据清单校验当前文件和备份的哈希，避免覆盖外部修改。

保存报告采用格式版本 2，每张图片的 `parameters` 记录实际处理参数。全部图片参数一致且完整时，顶层 `parameter_mode` 为 `uniform`，`parameters` 为共同参数；否则为 `per_image`，顶层 `parameters` 为 `null`，需读取逐图记录。`batch_context_parameters` 仅记录任务默认参数，未应用的界面草稿不计入已处理参数。恢复支持格式版本 1、2 的备份清单。

处理、扫描和保存均在后台运行。取消会等待当前图片结束；关闭窗口会等待后台任务或回滚完成，再清理临时缓存。客户端按图缓存结果，只加载当前预览；批量计算采用有限并发。

准备本地示例并直接打开：

```sh
.venv/bin/python tools/prepare_client_examples.py
.venv/bin/python app.py --gui --examples
```

默认抽取最多 16 张工作区中的多种字形、尺寸和纹理，包含可用红字的原尺寸和小尺寸尖细收笔案例，以及“朝”的原尺寸和小尺寸两张明暗不均案例，原图只复制。示例位于 `output/client-examples/`，点击“加载示例”即可导入；`expected/` 是同参数的候选输出，`preview-*.jpg` 是对照图，不代表人工真值。输出目录已存在时使用 `-o output/examples-new` 保留旧示例。其他机器可通过 `--source /path/to/images --count 12` 指定自己的图片。

## 平台接入

`ui/platform_integration.py` 提供共享认证、窗口保护和原生更新接入，按需导入共享包；独立产品标识为 `hanzi-mask`。`ui/window.py` 的桌面启动函数依次调用 `start_platform_session` 与 `attach_platform_services`：GUI 启动先完成共享登录（取消登录即退出，不进入本地窗口），登录成功后挂接授权守卫与原生更新。命令行入口保持本地行为，不加载共享模块。

`hanzi-mask` 已在 `hanzi-update` 的 products.json 注册表与服务端登记；v1.0.0 起提供签名安装包与稳定更新源，客户端通过“检查更新”获取后续版本。平台依赖使用 `vendor/hanzi_auth-0.2.11-py3-none-any.whl` 与 `vendor/hanzi_update-0.1.8-py3-none-any.whl`，从本项目目录安装 `requirements.txt` 后再安装 `.[platform]` 即可使用本地 wheel。

锁定救援保存将已采用的缓存结果固定导出为副本。锁定时协作取消计算并等待任务结束，保留结果缓存和人工清理的撤销记录；更新时，有采用结果则先在后台导出，成功完成并核对保存范围后才继续退出。原生更新准备期间的试探关闭保留缓存，实际关闭仍等待后台任务完成。

共享依赖由 `hanzi-auth/tools/sync_desktop.py` 维护，目前同步目标为字符检测和字形归并两个仓库。更新本项目 wheel 时需同步核对依赖引用与包版本；将本项目加入自动同步前，需明确 wheel 和打包脚本各自的目的地。当前沿用已分发的 wheel，不自动重建共享源码。

## 命令行使用

带参数运行 `app.py` 保留原命令行行为，`python -m hanzi_mask` 始终是命令行入口：

```sh
.venv/bin/python app.py ../10-1dbe -o output/10-1dbe-texture --method texture --preview
```

输出文件夹包含：

- 与输入对应的 `.png`：8 位灰度图，像素仅为 `0` 和 `255`。
- `preview-<运行编号>-<页码>.jpg`：原图、字形候选、变化区域三栏对照，每页最多 12 张。红色表示相对原始明暗候选删除的区域，青色表示纹理平滑或人工掩膜增加的区域。
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

对照原有几何筛选方法：

```sh
.venv/bin/python app.py ../10-1dbe -o output/10-1dbe-morphology --method morphology --preview
```

| 参数 | 作用 |
| --- | --- |
| `--method texture/morphology` | 默认 `texture` 使用纹理分离；`morphology` 使用原有几何筛选，速度更快 |
| `--profile gentle/balanced/strong` | 选择平滑、阈值偏置和几何筛选强度，默认 `balanced` |
| `--texture-strength 0.012` | 平滑强度，范围 `0–0.05`；仅 `texture` 使用，默认按 profile 选择 |
| `--texture-scale 3` | 要抑制的纹理尺度，范围 `0.5–12` 像素；仅 `texture` 使用 |
| `--keep-isolated` | 保留通过面积筛选的远离主体的小区域，适用于有独立点画的字 |
| `--stroke-radius 4` | 最小笔画尺度的圆盘半径，单位像素；越大越容易删除细笔画；`0` 关闭宽度筛选 |
| `--min-area 300` | 删除面积小于 300 像素的独立区域；可能误删点画；`0` 关闭面积筛选 |
| `--median-size 1` | 关闭默认的 3×3 中值滤波，保留更细的原始信息 |
| `--restore-steps 1` | 在处理方法生成的候选范围内恢复一层边缘，可能重新带回粘连碎斑；默认 `0` |
| `--foreground dark` | 处理浅底深字，导出仍为黑底白字；拓片默认 `light` |
| `--threshold 130` | 指定处理后图像的分界值，覆盖自动阈值及偏置；`dark` 时按反相后的亮度计算 |
| `--recursive` | 扫描子目录并保留相对路径，排除输出和人工掩膜目录 |

单图调参例子：

```sh
.venv/bin/python app.py ../10-1dbe/10-1dbe_crop_17.png \
  -o output/single-17 --stroke-radius 4 --min-area 500 --preview
```

`texture` 保留通过面积筛选的大部件，并根据与大部件的距离判断较小的孤立区域；被删除的小区域也可能是合法点画，报告会提示复核，可加 `--keep-isolated` 关闭此步骤。`morphology` 保留所有通过宽度和面积筛选的部件。如果同目录中 `字.png` 和 `字.jpg` 转换后都叫 `字.png`，工具会在导出前报错，需先重命名。

## 逐图人工掩膜

需要精确结果的图片，可以在图像编辑器中描出真实笔画，保存同尺寸的黑底白字 PNG。例如原图为 `10-1dbe_crop_74.png`，人工掩膜放在 `masks/10-1dbe_crop_74.png`：

```sh
.venv/bin/python app.py ../10-1dbe -o output/reviewed --mask-dir masks --preview
```

提供掩膜的图片直接使用人工掩膜，不再进行自动去噪。未提供掩膜的图片继续自动处理；错误尺寸会报错。掩膜按照灰度 `>127` 作为前景，透明区域按黑色背景处理。子目录中的人工掩膜也按输入的相对路径排列。

## 算法与适用范围

1. 按 EXIF 校正方向、处理透明背景并转换灰度，进行可调中值滤波。
2. `texture` 依据局部梯度与较大尺度的方向变化，迭代分离纹理与结构。内部工作图最长边限制为 512 像素，导出恢复原图尺寸。
3. 在结构图上估计明暗分界，并偏向较亮的笔画内部生成种子。
4. 桌面客户端在局部结构与原始候选证据约束下，重建与亮种子连通的较暗区域。
5. 使用圆盘开运算和区域面积筛除细小残留，按点画选项决定是否筛除较远的小孤岛。
6. 桌面客户端按原图信号在边界一像素窄带内细化，核对部件和孔洞。
7. 桌面客户端在处理末尾根据原图保护短小尖细收笔，输出掩膜、变化对照及参数记录。

命令行默认采用步骤 1、2、3、5；桌面的结构重建、边缘细化与笔尖保护采用内部固定参数。报告区分结构重建时的新增量和自动处理结束后的存留量；这些数量不代表分割准确率。

`morphology` 在中值滤波后直接生成明暗候选，再做宽度与面积筛选。两种方法都支持有限步边缘恢复；`texture` 的恢复受结构图候选约束。结构平滑可能填入原图的细小暗区，应检查对照图中的青色增加区域。

纹理分离借鉴 [Xu 等人的 RTV 结构提取研究](https://www.cse.cuhk.edu.hk/~leojia/projects/texturesep/)，采用本项目的灰度适配实现。Otsu 和开运算的定义见 [OpenCV 阈值文档](https://docs.opencv.org/4.x/d7/d4d/tutorial_py_thresholding.html)和[形态学文档](https://docs.opencv.org/4.x/d9/d61/tutorial_py_morphological_ops.html)。

同色、同宽且与笔画粘连的斑点，缺少可以由当前几何规则区分的信息。对于需要大批量精确分离真实字形的任务，应先建立人工校对的像素级标注，再训练专门的笔画/背景分割模型，并用独立标注集评估笔画保留率和斑点误留情况。本项目的候选掩膜可作为人工标注的起点，不应直接当作真实标签。

## 原生发布构建

[Build native installers](https://github.com/Normalight/hanzi-mask/actions/workflows/native-build.yml) 手动触发，输入版本号（需先提交对应 `RELEASE_NOTES_v<版本>.md`）并可选平台。流程下载并校验 WinSparkle / Sparkle 官方 SDK，配置 `packaging/native_update_public.json` 的更新源与公钥后构建：Windows x64 产出 Inno Setup 安装器并做安装后冒烟检查，macOS arm64 与 x86_64 产出含 Sparkle 的更新 ZIP。默认仅上传 Artifact；勾选 `publish` 后用仓库 Secrets 签名并发布（需要 `HANZI_UPDATE_PRIVATE_KEY`、`PUBLIC_RELEASE_TOKEN`、`GITCODE_TOKEN`）。也可本机签名后 `packaging/native_publish.py --github-only` 发布 GitHub 侧，GitCode 与平台版本登记由认证服务镜像器自动完成。

## 桌面构建 CI

Windows 压缩使用 Python `shutil.make_archive`，按需启用 Zip64。Mac 使用 `ditto` 保留应用元数据：当源文件总大小达到 4 GiB 或源条目数达到 65,535 时提前停止，不依赖其生成 Zip64。两平台生成 ZIP 后逐文件核对源大小并检查 CRC，通过后才生成流式 SHA-256 校验文件和上传；额外元数据或压缩开销造成的问题由成品检查发现。大包不会被整包读入内存计算摘要。

[Build desktop apps](https://github.com/Normalight/hanzi-mask/actions/workflows/build.yml) 仅支持手动 `workflow_dispatch`，可选择三个平台或单个平台；提交、PR、标签和 Release 不自动启动。使用 Python 3.11，在 Windows x64（`windows-2022`）、macOS arm64（`macos-15`）和 macOS x86_64（`macos-15-intel`）执行依赖及 wheel 校验、测试、Cython 保护、PyInstaller 打包和独立目录下的成品启动检查。运行器架构见 [GitHub 官方说明](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)。

版本读取 `pyproject.toml`。成品为 `字形换底-v<版本>-windows-x64.zip`（解压后运行 `hanzi-mask/hanzi-mask.exe`）、`字形换底-v<版本>-macos-arm64.zip` 与 `字形换底-v<版本>-macos-x86_64.zip`（内含 `字形换底.app`），附 SHA-256 校验文件，Actions 保留 14 天。Mac 使用 ad hoc 签名，未进行 Developer ID 签名或公证。桌面启动已接入共享登录与更新，成品冒烟检查仍只验证本地功能；工作流仅上传构建产物，没有发布步骤或发布密钥依赖。

构建依赖位于 `packaging/requirements-build.txt`，Mac/Windows 文件引用它。独立克隆本仓库即可构建，不读取相邻源码或本地测试图片；不打包训练模型。打包后的默认备份/导出目录为用户文稿目录下的 `字形换底/`，文稿目录不可用时退回用户主目录；源码运行沿用项目 `output/字形换底/`。

只检查配置（不构建）：

```sh
.venv/bin/python packaging/build_desktop.py --check --target macos-arm64
```

实际构建需在目标平台安装 `packaging/requirements-mac.txt` 或 `packaging/requirements-windows.txt`，并具备 Xcode Command Line Tools 或 MSVC C++ Build Tools，再执行 `python packaging/build_desktop.py --target <平台>`。构建脚本完成打包、成品检查和压缩；源码测试由 CI 单独执行。

`packaging/protection.py` 是 `hanzi-auth/tools/desktop_packaging/protection.py` 的原样副本，源摘要为 `5045e63416373304b40b7f74e3e69876e952a539e4e87abdac201e8bc0e733a3`。公共保护逻辑在源头维护；本项目尚未加入共享同步清单，更新时显式复制并验证，不能独立改此副本。其余打包入口和启动检查为本产品维护。

## 开发测试

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
```

也可以安装项目并使用 `hanzi-mask` 命令：

```sh
.venv/bin/python -m pip install -e .
.venv/bin/hanzi-mask --help
.venv/bin/hanzi-mask-desktop
```

退出码：`0` 表示处理完成（包含跳过已有输出），`1` 表示存在文件处理或写入失败，`2` 表示参数或路径错误。单图失败会记录在报告中，继续处理其他图片。

`ui/window.py` 负责桌面流程，`ui/canvas.py` 负责适配预览与色块选择，`ui/task_worker.py` 负责后台任务，`ui/platform_integration.py` 负责可选平台适配；`hanzi_mask/batch_service.py` 负责桌面批处理、缓存、导出、备份替换和恢复，`hanzi_mask/manual_correction.py` 负责独立的人工矫正缓存版本。`hanzi_mask/processing.py` 负责处理流程，`hanzi_mask/texture.py` 负责纹理分离与空间区域筛选，`hanzi_mask/stroke_recovery.py` 负责明暗不均结构的受约束重建，`hanzi_mask/edge_refinement.py` 负责原图引导的边缘细化，`hanzi_mask/tip_recovery.py` 负责处理末尾的笔尖保护，`hanzi_mask/cli.py` 负责命令行批处理，`hanzi_mask/preview.py` 负责对照图。`tests/` 覆盖算法规则、异步关闭、色块选择与撤销、复核选择、导出、替换回滚及恢复。合成测试验证处理规则，真实图片效果需逐图查看；尚未对真实数据评估分割准确率。
