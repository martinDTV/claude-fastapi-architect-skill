"""Users router (hand-written). NOT adopted in the tests: it must never change."""
from fastapi import APIRouter, HTTPException

from app import storage
from app.schemas import UserIn, UserOut

router = APIRouter(tags=["users"])


@router.post("", response_model=UserOut, status_code=201)
def create_user(payload: UserIn):
    user_id = storage.next_user_id()
    user = {"id": user_id, **payload.model_dump()}
    storage.USERS[user_id] = user
    return user


@router.get("/{user_id}", response_model=UserOut)
def get_user(user_id: int):
    if user_id not in storage.USERS:
        raise HTTPException(status_code=404, detail="no such user")
    return storage.USERS[user_id]
