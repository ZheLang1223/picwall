"""浏览器回归用临时相册；退出销毁，永不读取或修改真实 data/。"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--role', choices=['admin', 'editor', 'viewer'], default='admin')
    parser.add_argument('--unpaired', action='store_true')
    parser.add_argument('--port', type=int, default=18081)
    parser.add_argument('--scenario', choices=['album','first-use','capture-partial'], default='album')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="picwall-ui-test-") as directory:
        os.environ["PICWALL_DATA_DIR"] = directory
        os.environ["WB_COOKIE"] = ""
        os.environ["X_CRAWL_INTERVAL_HOURS"] = "0"
        import uvicorn

        from app import auth, crawler, db, demo, server

        db.init_db()
        if args.scenario != 'first-use':
            demo.seed()
            template = db.get_post("demo0000")
            for i in range(8, 120):
                p = dict(template, sid=f"demo{i:04}", fetched_at=template["fetched_at"])
                db.insert_post(p, "demo")
                for pc in template["pics"]:
                    db.insert_pic(p["sid"], pc["file"], pc["thumb"], pc["w"], pc["h"], pc["url"])
        if args.scenario == 'capture-partial':
            import json
            with db.conn() as c:
                c.execute("UPDATE pics SET quality='mid' WHERE sid='demo0000'")
            group = {'name':'演示分组','group':'演示分组','status':'done','received':20,
                     'accepted':15,'skipped':4,'failed':1,'partial':1,'unprocessed':0,
                     'new_posts':4,'new_pics':7,'coverage':'page_limit','page_limit':2}
            progress = {'state':'complete','running':False,'outcome':'partial','total':1,
                        'new_posts':4,'new_pics':7,'groups':[group],
                        'error':'部分图片待补抓；达到翻页上限，本轮尚未覆盖完整日期范围'}
            crawler.queue_posts([{**template, 'pics':[{'full':'demo://01','mid':'demo://01'}]}], 'demo')
            db.set_meta('crawl_checkpoint',json.dumps(progress,ensure_ascii=False))
            db.set_meta('last_result',json.dumps(progress,ensure_ascii=False))
            db.set_meta('last_error',progress['error'])
        if not args.unpaired:
            # 仅此临时夹具预配对，正式 app 不含绕过；负向边界由隔离 API 测试覆盖。
            token = auth.exchange(auth.issue_code(args.role), '临时演示', 'preview')['token']

            @server.app.middleware('http')
            async def paired_fixture(request, call_next):
                request.scope['headers'] = [(k, v) for k, v in request.scope['headers'] if k != b'cookie'] + [
                    (b'cookie', f'{auth.COOKIE}={token}'.encode())]
                return await call_next(request)

        uvicorn.run(server.app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
