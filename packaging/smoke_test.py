"""Run inside the bundle, from a directory without source or user images."""
from pathlib import Path
import sys
import tempfile


def smoke_test():
    if not getattr(sys, 'frozen', False):
        raise RuntimeError('启动检查必须使用打包后的可执行文件')
    import numpy as np
    from PIL import Image, ImageDraw
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QImageReader
    from ui.window import MainWindow
    from hanzi_mask.processing import process_image

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.show()
        app.processEvents()
        if not window.isVisible():
            raise RuntimeError('桌面窗口未打开')
        if any(name.split('.')[0] in {'hanzi_auth', 'hanzi_update', 'torch'}
               for name in sys.modules):
            raise RuntimeError('本地启动加载了平台或训练模块')
        if Path(window.output.text()).is_relative_to(Path(sys._MEIPASS)):
            raise RuntimeError('默认保存目录位于应用资源内')
        with tempfile.TemporaryDirectory(prefix='字形换底-启动检查-') as temporary:
            root = Path(temporary)
            image = Image.new('RGB', (80, 96), '#222222')
            draw = ImageDraw.Draw(image)
            draw.rectangle((15, 24, 65, 36), fill='white')
            draw.rectangle((34, 12, 46, 80), fill='white')
            for extension in ('png', 'jpg', 'bmp', 'tiff', 'webp'):
                path = root / f'图片.{extension}'
                image.save(path)
                if QImageReader(str(path)).read().isNull():
                    raise RuntimeError(f'图片解码失败：{extension}')
            result = process_image(root / '图片.png', window._all_options)
            if result.mask.shape != (96, 80) or result.mask.dtype != np.uint8:
                raise RuntimeError('纹理处理改变了图像尺寸或格式')
            if not np.any(result.mask) or not set(np.unique(result.mask)) <= {0, 255}:
                raise RuntimeError('纹理处理未生成黑白字形')
            Image.fromarray(result.mask).save(root / '结果.png')
    finally:
        window.close()
        app.processEvents()
