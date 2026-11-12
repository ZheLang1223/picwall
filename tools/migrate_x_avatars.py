"""为存量 X 帖回填作者头像（一次性迁移）。

背景：2026-10-16 起的 X 用户抓取初期，头像解析没读到新版响应的
result.avatar.image_url，入库的 X 帖 avatar 全空，博主面板没有头像。
本脚本对每个缺头像的 X 作者调 UserByScreenName 取当前头像，下载落盘
（storage.save_avatar，与抓取链路同一存储约定）后改写该作者全部 X 帖的
avatar 字段。幂等：已有头像的帖子跳过。

宿主运行（进容器没意义，容器是服务本体）：
    PICWALL_DATA_DIR=$PWD/data https_proxy=http://127.0.0.1:7897 \\
        .venv/bin/python tools/migrate_x_avatars.py
"""
import asyncio
import sys

import httpx

from app import config, db, media_fetch, storage
from app.sources import xapi

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


async def main() -> int:
    with db.conn() as c:
        rows = c.execute(
            "SELECT uid, MAX(handle) handle, MAX(avatar) avatar FROM posts "
            "WHERE source='x' GROUP BY uid").fetchall()
    todo = [r for r in rows if not r["avatar"]]
    print(f"X 作者 {len(rows)} 位，缺头像 {len(todo)} 位")
    if not todo:
        return 0
    token, ct0 = config.get_x_auth(), config.get_x_csrf()
    if not token:
        print("没有 X Cookie（data/x_auth.txt 缺失）", file=sys.stderr)
        return 2
    fixed = failed = 0
    async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                 proxy=config.X_PROXY_URL or None) as client:
        api = xapi.XGraphQL(token, ct0)
        for r in todo:
            handle = (r["handle"] or "").removeprefix("x:").lstrip("@")
            try:
                params = {
                    "variables": xapi._json({"screen_name": handle,
                                             "withGrokTranslatedBio": False}),
                    "features": xapi._json(xapi.FEATURES),
                    "fieldToggles": xapi._json({"withAuxiliaryUserLabels": True}),
                }
                resp = await client.get("https://x.com/i/api" + xapi.ENDPOINTS["user_by"],
                                        params=params, headers=api.headers)
                resp.raise_for_status()
                result = resp.json()["data"]["user"]["result"]
                url = ((result.get("avatar") or {}).get("image_url")
                       or (result.get("legacy") or {}).get("profile_image_url_https") or "")
                if not url:
                    raise ValueError("响应里没有头像")
                data = await media_fetch.fetch(client, url, {"User-Agent": UA})
                if not data:
                    raise ValueError("头像下载失败")
                rel = storage.save_avatar(data, url)
                with db.conn() as c:
                    c.execute("UPDATE posts SET avatar=? WHERE source='x' AND uid=?",
                              (rel, r["uid"]))
                fixed += 1
                print(f"  ✓ @{handle} -> {rel}")
            except Exception as e:
                failed += 1
                print(f"  ✗ @{handle}（uid={r['uid']}）: {type(e).__name__}: {e}")
    print(f"完成：成功 {fixed}，失败 {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    config.ensure_dirs()
    sys.exit(asyncio.run(main()))
