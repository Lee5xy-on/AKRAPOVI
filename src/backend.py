#!/usr/bin/env python3
"""
종단간 암호화 채팅 서버
- 중복 닉네임 방지
- 메시지 200자 제한
"""

import asyncio
import json
import os
import uuid
import logging
from datetime import datetime
from typing import Dict, Optional
import websockets
from websockets.server import WebSocketServerProtocol

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# { room_id: { websocket: { username, public_key, profile, joined_at } } }
rooms: Dict[str, Dict] = {}

MAX_MSG_LEN = 200


def get_all_usernames() -> set:
    names = set()
    for room in rooms.values():
        for info in room.values():
            names.add(info['username'])
    return names


async def send_error(websocket: WebSocketServerProtocol, code: str, message: str):
    await websocket.send(json.dumps({
        "type": "error",
        "code": code,
        "message": message
    }))
    await websocket.close()


async def register_client(
    websocket: WebSocketServerProtocol,
    room_id: str,
    username: str,
    public_key: str,
    profile: dict,
) -> bool:

    # ── 닉네임 중복 체크 ──────────────────────────────
    if username in get_all_usernames():
        logger.warning(f"닉네임 중복: '{username}' 거부")
        await send_error(
            websocket,
            "USERNAME_TAKEN",
            f"'{username}' 닉네임은 이미 사용 중입니다. 다른 닉네임을 선택해주세요."
        )
        return False

    # ── 방 생성 & 등록 ───────────────────────────────
    if room_id not in rooms:
        rooms[room_id] = {}

    rooms[room_id][websocket] = {
        "username":   username,
        "public_key": public_key,
        "profile":    profile,
        "joined_at":  datetime.now().isoformat()
    }

    logger.info(f"[{room_id}] ✅ '{username}' 입장 (총 {len(rooms[room_id])}명)")

    # ── 기존 사용자 목록 전송 ─────────────────────────
    existing_users = []
    for ws, info in rooms[room_id].items():
        if ws != websocket:
            existing_users.append({
                "username":   info["username"],
                "public_key": info["public_key"],
                "profile":    info.get("profile", {})
            })

    await websocket.send(json.dumps({
        "type":           "room_info",
        "room_id":        room_id,
        "existing_users": existing_users
    }))

    # ── 다른 사용자에게 입장 알림 ─────────────────────
    await broadcast(room_id, json.dumps({
        "type":       "user_joined",
        "username":   username,
        "public_key": public_key,
        "profile":    profile,
        "timestamp":  datetime.now().isoformat()
    }), exclude=websocket)

    return True


async def unregister_client(websocket: WebSocketServerProtocol, room_id: str):
    if room_id not in rooms or websocket not in rooms[room_id]:
        return

    info     = rooms[room_id][websocket]
    username = info["username"]

    del rooms[room_id][websocket]

    if not rooms[room_id]:
        del rooms[room_id]
        logger.info(f"[{room_id}] 방 삭제됨")
    else:
        logger.info(f"[{room_id}] '{username}' 퇴장 (남은 인원: {len(rooms[room_id])}명)")
        await broadcast(room_id, json.dumps({
            "type":      "user_left",
            "username":  username,
            "timestamp": datetime.now().isoformat()
        }))


async def broadcast(room_id: str, message: str, exclude: WebSocketServerProtocol = None):
    if room_id not in rooms:
        return
    disconnected = []
    for ws in list(rooms[room_id]):
        if ws == exclude:
            continue
        try:
            await ws.send(message)
        except websockets.exceptions.ConnectionClosed:
            disconnected.append(ws)
    for ws in disconnected:
        rooms[room_id].pop(ws, None)


async def handle_client(websocket: WebSocketServerProtocol):
    room_id:  Optional[str] = None
    username: Optional[str] = None

    logger.info(f"새 연결: {websocket.remote_address}")

    try:
        async for raw_message in websocket:
            try:
                data     = json.loads(raw_message)
                msg_type = data.get("type")

                # ── JOIN ──────────────────────────────────────
                if msg_type == "join":
                    req_room = data.get("room_id", "").strip() or str(uuid.uuid4())[:8].upper()
                    req_name = data.get("username", "").strip()
                    pub_key  = data.get("public_key", "")
                    profile  = data.get("profile", {})

                    if not req_name:
                        await send_error(websocket, "INVALID_USERNAME", "닉네임을 입력해주세요.")
                        return

                    ok = await register_client(websocket, req_room, req_name, pub_key, profile)
                    if ok:
                        room_id  = req_room
                        username = req_name

                # ── MESSAGE ───────────────────────────────────
                elif msg_type == "message":
                    if not (room_id and username):
                        continue

                    if len(raw_message) > 32_768:
                        await websocket.send(json.dumps({
                            "type":    "error",
                            "code":    "MSG_TOO_LONG",
                            "message": "메시지가 너무 깁니다."
                        }))
                        continue

                    await broadcast(room_id, json.dumps({
                        "type":               "message",
                        "from":               username,
                        "encrypted_messages": data.get("encrypted_messages", {}),
                        "timestamp":          datetime.now().isoformat(),
                        "msg_id":             str(uuid.uuid4())
                    }))

                # ── PING ──────────────────────────────────────
                elif msg_type == "ping":
                    await websocket.send(json.dumps({"type": "pong"}))

            except json.JSONDecodeError:
                logger.warning("잘못된 JSON")
                await websocket.send(json.dumps({"type": "error", "code": "BAD_JSON", "message": "Invalid JSON"}))

    except websockets.exceptions.ConnectionClosed:
        logger.info(f"연결 종료: {username or '?'}")
    finally:
        if room_id:
            await unregister_client(websocket, room_id)


async def main():
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", 8765))

    logger.info("🔐 종단간 암호화 채팅 서버 시작")
    logger.info(f"📡 WebSocket: ws://{host}:{port}")
    logger.info("🚫 중복 닉네임 / 200자 제한 적용")
    logger.info("⚠️  서버는 암호화된 데이터만 중계합니다")

    async with websockets.serve(handle_client, host, port):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("서버 종료")
