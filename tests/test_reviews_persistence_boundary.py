"""Характеризационные тесты persistence-границы отзывов (workstream D)."""
import pytest

from database import orders, reviews

CLIENT_TG = 7770001
MASTER_A = 999001
MASTER_B = 999002


async def prepare(db):
    await db.execute(
        "INSERT INTO users(id, telegram_id, is_registered) VALUES (1, ?, 1)",
        (CLIENT_TG,),
    )
    await db.execute(
        "INSERT INTO masters(telegram_id, full_name, is_active) VALUES (?, 'A', 1)",
        (MASTER_A,),
    )
    await db.execute(
        "INSERT INTO masters(telegram_id, full_name, is_active) VALUES (?, 'B', 1)",
        (MASTER_B,),
    )
    # Заказ формально назначен мастеру A, но отзыв исторически указывает на B.
    # Это позволяет доказать, что summary-функции не эквивалентны.
    await db.execute(
        """
        INSERT INTO orders(
            id, user_id, direction, subject, work_type, description,
            status, assigned_master_telegram_id
        ) VALUES (1, 1, 'Математика', 'Задача', 'solution', 'решить',
                  'completed', ?)
        """,
        (MASTER_A,),
    )
    await db.execute(
        """
        INSERT INTO reviews(order_id, client_user_id, master_telegram_id, rating, review_text)
        VALUES (1, 1, ?, 4, 'текст')
        """,
        (MASTER_B,),
    )
    await db.commit()


def test_orders_keeps_compatibility_reexports():
    for name in (
        "create_review", "get_review_by_order_id", "get_client_review_for_order",
        "add_review_text", "get_all_reviews", "get_reviews_by_master",
        "get_master_reviews", "get_master_rating_summary",
        "get_masters_with_reviews", "get_masters_with_reviews_count",
    ):
        assert getattr(orders, name) is getattr(reviews, name)


@pytest.mark.asyncio
async def test_distinct_rating_semantics_are_not_unified(db):
    await prepare(db)

    # orders.assigned_master_telegram_id = A -> учитывается в summary A.
    assert await reviews.get_master_reviews_summary(MASTER_A) == (1, 4.0)
    assert await reviews.get_master_reviews_summary(MASTER_B) == (0, None)

    # reviews.master_telegram_id = B -> учитывается в rating summary B.
    row_a = await reviews.get_master_rating_summary(MASTER_A)
    row_b = await reviews.get_master_rating_summary(MASTER_B)
    assert (row_a["reviews_count"], row_a["average_rating"]) == (0, None)
    assert (row_b["reviews_count"], row_b["average_rating"]) == (1, 4.0)

    # get_master_rating_stats идёт через orders.assigned_master_telegram_id,
    # поэтому для A он видит отзыв, а для B — нет (семантика сохранена).
    stats_a = await reviews.get_master_rating_stats(MASTER_A)
    assert stats_a["reviews_count"] == 1
    assert stats_a["rating_4"] == 1
    assert stats_a["average_rating"] == 4.0
    stats_b = await reviews.get_master_rating_stats(MASTER_B)
    assert stats_b["reviews_count"] == 0


@pytest.mark.asyncio
async def test_create_review_requires_completed_owned_order_and_is_idempotent(db):
    await prepare(db)

    assert await reviews.create_review(1, CLIENT_TG, 5) is False  # already reviewed
    assert tuple(await (await db.execute(
        "SELECT rating FROM reviews WHERE order_id=1"
    )).fetchone()) == (4,)

    await db.execute("DELETE FROM reviews")
    await db.execute("UPDATE orders SET status='new' WHERE id=1")
    await db.commit()
    assert await reviews.create_review(1, CLIENT_TG, 5) is False

    await db.execute("UPDATE orders SET status='completed' WHERE id=1")
    await db.commit()
    assert await reviews.create_review(1, 9999999, 5) is False
    assert await reviews.create_review(1, CLIENT_TG, 5) is True
    assert await reviews.create_review(1, CLIENT_TG, 4) is False
    assert tuple(await (await db.execute(
        "SELECT rating, master_telegram_id FROM reviews WHERE order_id=1"
    )).fetchone()) == (5, MASTER_A)


@pytest.mark.asyncio
async def test_review_listing_shapes_remain_compatible(db):
    await prepare(db)

    by_order = await reviews.get_all_reviews()
    assert len(by_order) == 1
    assert by_order[0]["client_bot_nickname"] is None
    assert by_order[0]["master_name"] == "B"

    by_master = await reviews.get_reviews_by_master(MASTER_B)
    assert len(by_master) == 1 and by_master[0]["order_id"] == 1

    raw_master_reviews = await reviews.get_master_reviews(MASTER_B)
    assert len(raw_master_reviews) == 1 and raw_master_reviews[0]["review_text"] == "текст"

    masters = await reviews.get_masters_with_reviews()
    assert [row["telegram_id"] for row in masters] == [MASTER_B]
    assert await reviews.get_masters_with_reviews_count() == 1
