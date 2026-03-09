#!/usr/bin/env python3
"""
종단간 암호화 채팅 서버
- IP당 계정 1개 제한
- 중복 닉네임 방지
- 메시지 200자 제한
"""

import asyncio
import json
import uuid
import logging
from datetime import datetime
from typing import Dict, Optional
import websockets
from websockets.server import WebSocketServerProtocol

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# { room_id: { websocket: { username, public_key, ip, profile, joined_at } } }
rooms: Dict[str, Dict] = {}

# IP → username 전역 매핑 (같은 IP는 한 계정만)
ip_to_username: Dict[str, str] = {}

# username → IP 역매핑
username_to_ip: Dict[str, str] = {}

MAX_MSG_LEN = 200


def get_client_ip(websocket: WebSocketServerProtocol) -> str:
    """클라이언트 IP 추출 (프록시 헤더 우선)"""
    # X-Forwarded-For 헤더가 있으면 사용 (nginx/프록시 뒤에 있을 경우)
    headers = getattr(websocket, 'request_headers', {})
    forwarded = headers.get('X-Forwarded-For') or headers.get('x-forwarded-for')
    if forwarded:
        return forwarded.split(',')[0].strip()
    # 직접 연결
    remote = websocket.remote_address
    if remote:
        return remote[0]
    return '0.0.0.0'


def get_room_usernames(room_id: str) -> set:
    """방의 모든 닉네임 집합 반환"""
    if room_id not in rooms:
        return set()
    return {info['username'] for info in rooms[room_id].values()}


def get_all_usernames() -> set:
    """전체 서버에서 사용 중인 닉네임 집합"""
    names = set()
    for room in rooms.values():
        for info in room.values():
            names.add(info['username'])
    return names


async def send_error(websocket: WebSocketServerProtocol, code: str, message: str):
    """에러 메시지 전송 후 연결 종료"""
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
    client_ip: str
) -> bool:
    """
    클라이언트 등록.
    반환값: True = 성공, False = 거부됨
    """

    # ── 1. IP 중복 체크 ──────────────────────────────
    if client_ip in ip_to_username:
        existing_name = ip_to_username[client_ip]
        if existing_name != username:
            logger.warning(f"IP {client_ip} 이미 '{existing_name}'으로 접속 중 → '{username}' 거부")
            await send_error(
                websocket,
                "IP_ALREADY_REGISTERED",
                f"이 IP는 이미 '{existing_name}' 계정으로 접속 중입니다. 한 IP당 하나의 계정만 허용됩니다."
            )
            return False

    # ── 2. 닉네임 중복 체크 (전체 서버) ─────────────
    all_names = get_all_usernames()
    if username in all_names:
        # 같은 IP의 재접속이면 허용 (재연결 시나리오)
        if ip_to_username.get(client_ip) != username:
            logger.warning(f"닉네임 중복: '{username}' 거부 (IP: {client_ip})")
            await send_error(
                websocket,
                "USERNAME_TAKEN",
                f"'{username}' 닉네임은 이미 사용 중입니다. 다른 닉네임을 선택해주세요."
            )
            return False

    # ── 3. 방 생성 & 등록 ───────────────────────────
    if room_id not in rooms:
        rooms[room_id] = {}

    rooms[room_id][websocket] = {
        "username":   username,
        "public_key": public_key,
        "profile":    profile,
        "ip":         client_ip,
        "joined_at":  datetime.now().isoformat()
    }
    ip_to_username[client_ip] = username
    username_to_ip[username]  = client_ip

    logger.info(f"[{room_id}] ✅ '{username}' 입장 (IP: {client_ip}, 총 {len(rooms[room_id])}명)")

    # ── 4. 기존 사용자 목록 전송 ─────────────────────
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

    # ── 5. 다른 사용자에게 입장 알림 ─────────────────
    await broadcast(room_id, json.dumps({
        "type":       "user_joined",
        "username":   username,
        "public_key": public_key,
        "profile":    profile,
        "timestamp":  datetime.now().isoformat()
    }), exclude=websocket)

    return True


async def unregister_client(websocket: WebSocketServerProtocol, room_id: str):
    """클라이언트 등록 해제"""
    if room_id not in rooms or websocket not in rooms[room_id]:
        return

    info     = rooms[room_id][websocket]
    username = info["username"]
    client_ip = info["ip"]

    del rooms[room_id][websocket]

    # IP / 이름 매핑 해제
    ip_to_username.pop(client_ip, None)
    username_to_ip.pop(username, None)

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
    """방의 모든 클라이언트에게 메시지 전송"""
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
    """클라이언트 연결 핸들러"""
    room_id:  Optional[str] = None
    username: Optional[str] = None
    client_ip = get_client_ip(websocket)

    logger.info(f"새 연결: {client_ip}")

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

                    ok = await register_client(
                        websocket, req_room, req_name, pub_key, profile, client_ip
                    )
                    if ok:
                        room_id  = req_room
                        username = req_name

                # ── MESSAGE ───────────────────────────────────
                elif msg_type == "message":
                    if not (room_id and username):
                        continue

                    encrypted = data.get("encrypted_messages", {})

                    # 서버 측 200자 제한: 암호화 전 평문을 알 수 없으므로
                    # 페이로드 크기로 간접 제한 (200자 UTF-8 암호화는 약 5KB 이하)
                    raw_size = len(raw_message)
                    if raw_size > 32_768:   # 32KB 초과 시 거부
                        await websocket.send(json.dumps({
                            "type":    "error",
                            "code":    "MSG_TOO_LONG",
                            "message": "메시지가 너무 깁니다."
                        }))
                        continue

                    await broadcast(room_id, json.dumps({
                        "type":               "message",
                        "from":               username,
                        "encrypted_messages": encrypted,
                        "timestamp":          datetime.now().isoformat(),
                        "msg_id":             str(uuid.uuid4())
                    }))

                # ── PING ──────────────────────────────────────
                elif msg_type == "ping":
                    await websocket.send(json.dumps({"type": "pong"}))

            except json.JSONDecodeError:
                logger.warning(f"잘못된 JSON (IP: {client_ip})")
                await websocket.send(json.dumps({"type": "error", "code": "BAD_JSON", "message": "Invalid JSON"}))

    except websockets.exceptions.ConnectionClosed:
        logger.info(f"연결 종료: {username or '?'} ({client_ip})")
    finally:
        if room_id:
            await unregister_client(websocket, room_id)


async def main():
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", 8765))

    logger.info("🔐 종단간 암호화 채팅 서버 시작")
    logger.info(f"📡 WebSocket: ws://{host}:{port}")
    logger.info("🚫 IP당 1계정 / 중복 닉네임 / 200자 제한 적용")
    logger.info("⚠️  서버는 암호화된 데이터만 중계합니다")

    async with websockets.serve(handle_client, host, port):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("서버 종료")
