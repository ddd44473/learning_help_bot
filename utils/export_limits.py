"""Локальные бюджеты выгрузки; превышение не маскируется усечением."""
import io

MAX_EXPORT_ROWS = 10000
MAX_EXPORT_DATA_BYTES = 4 * 1024**2
MAX_EXPORT_CELL_CHARS = 65536
MAX_EXPORT_OUTPUT_BYTES = 5 * 1024**2


class ExportLimitError(ValueError):
    pass


def checked_output(data: bytes) -> bytes:
    if len(data) > MAX_EXPORT_OUTPUT_BYTES:
        raise ExportLimitError("Файл превышает локальный лимит выгрузки.")
    return data


async def bounded_rows(cursor):
    try:
        rows, size = [], 0
        while True:
            chunk = await cursor.fetchmany(min(32, MAX_EXPORT_ROWS + 1))
            if not chunk:
                break
            for row in chunk:
                for value in row:
                    if value is not None:
                        text = str(value)
                        if len(text) > MAX_EXPORT_CELL_CHARS:
                            raise ExportLimitError("Запись слишком большая для выгрузки.")
                        size += len(text.encode('utf-8'))
                if size > MAX_EXPORT_DATA_BYTES:
                    raise ExportLimitError("Выгрузка превышает локальный лимит данных. Выберите меньший период.")
                rows.append(row)
                if len(rows) > MAX_EXPORT_ROWS:
                    raise ExportLimitError("Слишком много записей для одной выгрузки. Выберите меньший период.")
        return rows
    finally:
        await cursor.close()


def safe_csv_cell(value):
    if isinstance(value, str) and value.lstrip('\ufeff\t\r\n ').startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


class LimitedTextIO(io.StringIO):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bytes_written = 0

    def write(self, value):
        self.bytes_written += len(value.encode('utf-8'))
        if self.bytes_written > MAX_EXPORT_OUTPUT_BYTES:
            raise ExportLimitError("Файл превышает локальный лимит выгрузки. Выберите меньший период.")
        return super().write(value)


class LimitedParts(list):
    def __init__(self, values=()):
        super().__init__()
        self.bytes_written = 0
        self.extend(values)

    def append(self, value):
        self.bytes_written += len(value.encode('utf-8')) + 1
        if self.bytes_written > MAX_EXPORT_OUTPUT_BYTES:
            raise ExportLimitError("HTML превышает локальный лимит выгрузки.")
        super().append(value)

    def extend(self, values):
        for value in values:
            self.append(value)

    def __iadd__(self, values):
        self.extend(values)
        return self
