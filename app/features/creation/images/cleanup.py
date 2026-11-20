"""CREATE-US-06-AC11 미사용 이미지 정리 실행.

실행: python -m app.features.creation.images.cleanup [--grace-hours 24]
정리 주기(예: 하루 한 번)는 아직 정하지 않았다. 지금은 필요할 때 직접 실행한다.
"""

import argparse
import logging
from datetime import timedelta

import psycopg
from psycopg.rows import dict_row

from app.core.config import settings
from app.features.creation.images import service
from app.features.creation.images.storage import LocalImageStorage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="연결이 없는 이미지 파일을 유예 기간 뒤 정리한다.")
    parser.add_argument("--grace-hours", type=float, default=settings.image_cleanup_grace_hours)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    if not settings.database_url:
        parser.error("DATABASE_URL을 설정하세요.")
    with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row) as conn:
        result = service.cleanup_unused_images(
            conn, LocalImageStorage(settings.media_root), timedelta(hours=args.grace_hours)
        )
    print(
        f"미사용 표시 {result.marked}, 표시 해제 {result.unmarked}, 삭제 {len(result.deleted)}, "
        f"유지 {len(result.kept)}, 실패 {len(result.failed)}"
    )
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
