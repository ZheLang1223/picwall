from app import auth


def admin_headers():
    result = auth.exchange(auth.issue_code("admin"), "隔离测试", "test")
    return {"Host": "localhost", "Origin": "http://localhost",
            "Authorization": "Bearer " + result["token"]}
