"""Локальная модель данных обёртки.

Keitaro — источник правды для опубликованного состояния. У нас хранится:
- кампании и потоки (id совпадают с id в Keitaro);
- привязки офферов к потокам в двух версиях: черновик (share/state)
  и последняя опубликованная (published_share/published_state).
  Поток «жёлтый» (не опубликован), если хоть одна привязка отличается
  (включая закрепление — иначе Cancel откатывал бы невидимые изменения).
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, false, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

ACTIVE = "active"
REMOVED = "removed"  # у нас в архиве, в Keitaro его нет
DISABLED = "disabled"  # выключен в самом Keitaro: не трогаем, но и не теряем при Push


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(255))
    alias: Mapped[str | None] = mapped_column(String(64))
    geo: Mapped[str | None] = mapped_column(String(255))
    created_by_wrapper: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    streams: Mapped[list["Stream"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", order_by="Stream.position"
    )


class Offer(Base):
    """Кэш названий офферов Keitaro."""

    __tablename__ = "offers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(255))


class Stream(Base):
    __tablename__ = "streams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255), default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    # schema из Keitaro: офферы бывают только у потоков "landings"
    schema: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Поток без офферов храним «как поток» и ничего про него не показываем
    has_offers: Mapped[bool] = mapped_column(Boolean, default=False)

    campaign: Mapped[Campaign] = relationship(back_populates="streams")
    offers: Mapped[list["StreamOffer"]] = relationship(
        back_populates="stream", cascade="all, delete-orphan", order_by="StreamOffer.id"
    )

    @property
    def dirty(self) -> bool:
        return any(so.dirty for so in self.offers)


class StreamOffer(Base):
    __tablename__ = "stream_offers"
    __table_args__ = (UniqueConstraint("stream_id", "offer_id", name="uq_stream_offer"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stream_id: Mapped[int] = mapped_column(ForeignKey("streams.id", ondelete="CASCADE"))
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id"))

    share: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(16), default=ACTIVE)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)

    # None — привязка ещё ни разу не публиковалась
    published_share: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Закрепление не уходит в Keitaro, но Cancel должен вернуть и его
    published_pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())

    stream: Mapped[Stream] = relationship(back_populates="offers")
    offer: Mapped[Offer] = relationship()

    @property
    def dirty(self) -> bool:
        return (
            self.share != self.published_share
            or self.state != self.published_state
            or self.pinned != self.published_pinned
        )
