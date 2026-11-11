"""Process-wide admission control, включая ожидание Telegram upload."""
from functools import wraps
from threading import BoundedSemaphore

from utils.export_limits import ExportLimitError

_slots = BoundedSemaphore(2)


def bounded_export(handler):
    @wraps(handler)
    async def wrapped(callback, *args, **kwargs):
        if not _slots.acquire(blocking=False):
            await callback.answer('Выгрузки заняты. Попробуйте немного позже.', show_alert=True)
            return
        try:
            return await handler(callback, *args, **kwargs)
        except ExportLimitError as error:
            await callback.answer(str(error), show_alert=True)
        finally:
            _slots.release()
    return wrapped
