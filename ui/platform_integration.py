"""Shared account and update bridge for the desktop client.

Importing this module does not load either shared SDK. The normal desktop and
CLI remains independent of the desktop startup integration.
"""

from PySide6.QtCore import QObject, QTimer, Slot

from hanzi_mask.batch_service import SaveSummary


PRODUCT = 'hanzi-mask'
_COMPUTE_OPERATIONS = frozenset({'scan', 'process', 'correct'})


def start_platform_session(app):
    """Acquire this product's instance and log in through the shared SDK."""
    from hanzi_auth import start_session
    return start_session(app, PRODUCT)


def attach_platform_services(window, session):
    """Attach account protection and native updates after a successful login."""
    if getattr(session, 'product', None) != PRODUCT:
        raise ValueError('登录会话不属于字形换底软件')
    from hanzi_auth import protect_window
    from hanzi_update import attach_updates

    services = PlatformServices(window)
    window._platform_services = services
    services.guard = protect_window(window, session, services.export_preserved_results,
                                    services.lock_session)
    services.updater = attach_updates(window, session.product,
                                      button=window.check_updates_button,
                                      is_locked=lambda: services.guard.locked,
                                      precheck=services.guard.check_native_update)
    return services


class PlatformServices(QObject):
    """Preserve cached work across authorization locks and update preparation.

    The host calls on_saved before clearing its worker, and on_task_finished
    after clearing it. Updates own only the export started by before_close.
    """

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.guard = None
        self.updater = None
        self._rescue_pending = False
        self._dispatch_scheduled = False
        self._update_export_active = False
        self._update_worker = None
        self._update_save_ready = False
        self._update_saved_items = ()
        self._update_saved_style = None
        self._update_close_pending = False

    def _locked(self):
        return bool(self.guard is not None and self.guard.locked)

    def _updater(self):
        return self.updater or getattr(self.window, '_update_controller', None)

    def permits(self, operation):
        return not self._locked() or operation == 'export'

    def _abort_update(self, message=None):
        updater = self._updater()
        if updater is not None:
            updater.restart_requested = False
        self._update_export_active = False
        self._update_worker = None
        self._update_save_ready = False
        self._update_saved_items = ()
        self._update_saved_style = None
        self._update_close_pending = False
        if message:
            self.window.status.setText(message)

    def _pause_computation(self):
        self.window._discard_pending_preview()
        worker = self.window.worker
        if worker is not None and self.window._task_operation in _COMPUTE_OPERATIONS:
            worker.requestInterruption()

    def lock_session(self):
        """Cancel new calculations; leave existing save transactions to finish."""
        self._pause_computation()
        self.window.preview.clear_selection()
        self._abort_update()

    def export_preserved_results(self):
        """Export adopted cached results, regardless of the current save mode."""
        if self.window._closing:
            return
        self._pause_computation()
        if self.window.worker is not None:
            self._rescue_pending = True
            return
        self._rescue_pending = False
        self.window.save_results(mode='export')

    def on_saved(self, summary: SaveSummary):
        """Only this update attempt's successful export can authorize restart."""
        if not self._update_export_active or self.window.worker is not self._update_worker:
            return
        updater = self._updater()
        success = not summary.cancelled and not summary.errors
        if updater is None or not updater.restart_requested or not success:
            self._abort_update()
            return
        self._update_save_ready = True

    def on_task_finished(self, operation):
        """Dispatch follow-up work after the host releases its worker reference."""
        if operation == 'export' and self._update_export_active:
            self._update_export_active = False
            self._update_worker = None
            updater = self._updater()
            if self._update_save_ready and updater is not None and updater.restart_requested:
                self._update_close_pending = True
            else:
                self._abort_update()
        if self._rescue_pending or self._update_close_pending:
            self._schedule_dispatch()

    def _schedule_dispatch(self):
        if not self._dispatch_scheduled:
            self._dispatch_scheduled = True
            QTimer.singleShot(0, self._dispatch_pending)

    @Slot()
    def _dispatch_pending(self):
        self._dispatch_scheduled = False
        if self.window._closing:
            self._rescue_pending = False
            self._abort_update()
            return
        if self.window.worker is not None:
            return
        if self._rescue_pending:
            self._rescue_pending = False
            self.export_preserved_results()
            # A queued update close waits for this explicit rescue export too.
            if self.window.worker is None and self._update_close_pending:
                self._schedule_dispatch()
            return
        if self._update_close_pending:
            self._update_close_pending = False
            updater = self._updater()
            if self._update_save_ready and updater is not None and updater.restart_requested:
                self.window.close()

    def _saved_selection_is_current(self):
        current = self.window.adopted_items()
        return (len(current) == len(self._update_saved_items)
                and self.window.output_style.currentData() == self._update_saved_style
                and all(item is saved for item, saved in zip(current, self._update_saved_items)))

    def _allow_close(self, event, updater):
        if updater.preparing:
            # NativeUpdateController probes closeEvent, then ignores the real
            # event until its installer is authorized. Keep cache files intact.
            event.accept()
            return False
        return True

    def before_close(self, event):
        """Return True for the host's actual close path, False if handled here."""
        updater = self._updater()
        if updater is None or not updater.restart_requested:
            return True
        if self._locked():
            self._abort_update()
            event.ignore()
            return False
        worker = self.window.worker
        if self.window._preview_workers:
            self._abort_update('请等待当前任务完成后，再保存并重启更新。')
            event.ignore()
            return False
        if worker is not None:
            if not (self._update_export_active and worker is self._update_worker
                    and self.window._task_operation == 'export'):
                self._abort_update('请等待当前任务完成后，再保存并重启更新。')
            event.ignore()
            return False
        if self._update_export_active:
            # A completion/finished signal is still queued in the host.
            event.ignore()
            return False
        # Clear automatic follow-ups only when update preparation can proceed.
        # A refused busy update must leave current work and its generation intact.
        self.window._discard_pending_preview(cancel_running=False)
        if self._update_save_ready and self._saved_selection_is_current():
            return self._allow_close(event, updater)
        adopted = tuple(self.window.adopted_items())
        self._update_save_ready = False
        self._update_saved_items = adopted
        self._update_saved_style = self.window.output_style.currentData()
        if not adopted:
            return self._allow_close(event, updater)
        self._update_export_active = True
        self.window.save_results(mode='export')
        if self.window.worker is not None and self.window._task_operation == 'export':
            self._update_worker = self.window.worker
        else:
            # Output validation or a user decision prevented the save task.
            self._abort_update()
        event.ignore()
        return False
