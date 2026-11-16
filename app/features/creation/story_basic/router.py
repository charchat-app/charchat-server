"""CREATE-US-01 엔드포인트. 엔드포인트 추가 시 이 파일에 라우트를 정의한다."""

from fastapi import APIRouter

router = APIRouter(tags=["creation:story_basic"])
