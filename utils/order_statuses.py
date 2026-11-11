ORDER_STATUS_NEW = "new"
ORDER_STATUS_IN_PROGRESS = "in_progress"
ORDER_STATUS_WAITING_CLIENT = "waiting_client"
ORDER_STATUS_COMPLETED = "completed"
ORDER_STATUS_CANCELLED = "cancelled"
ORDER_STATUS_CLOSED = "closed"


ACTIVE_ORDER_STATUSES = {
    ORDER_STATUS_NEW,
    ORDER_STATUS_IN_PROGRESS,
    ORDER_STATUS_WAITING_CLIENT,
}


FINAL_ORDER_STATUSES = {
    ORDER_STATUS_COMPLETED,
    ORDER_STATUS_CANCELLED,
    ORDER_STATUS_CLOSED,
}


ORDER_STATUS_TITLES = {
    ORDER_STATUS_NEW: "Новый",
    ORDER_STATUS_IN_PROGRESS: "В работе",
    ORDER_STATUS_WAITING_CLIENT: "Ожидает подтверждения клиента",
    ORDER_STATUS_COMPLETED: "Завершён",
    ORDER_STATUS_CANCELLED: "Отменён",
    ORDER_STATUS_CLOSED: "Закрыт",
}


def is_order_active(status: str) -> bool:
    return status in ACTIVE_ORDER_STATUSES


def is_order_final(status: str) -> bool:
    return status in FINAL_ORDER_STATUSES


def get_order_status_title(status: str) -> str:
    return ORDER_STATUS_TITLES.get(
        status,
        f"Неизвестный статус: {status}",
    )
