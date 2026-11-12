"""Source 适配层：把「外部平台 → 统一帖子模型」的边界收敛在这里。

所有来源（weibo / x / 未来更多）产出同一套 post dict，交给 crawler._store_post
统一下载落盘入库；图片墙、保留/删除/收藏、下载系统只认库内模型，不感知来源。

统一 post dict 契约（POST_CONTRACT）：
  sid          来源内唯一 ID；跨来源防撞 = 非微博来源带前缀（x:<tweet_id>），微博存量不加
  source       来源标识（weibo / x / …）
  uid, author, avatar, text, created_at(ISO+08:00), fetched_at
  weibo_url    原帖链接（历史列名，语义 = originalUrl）
  via          转发者昵称（无转发为空）
  is_long      0/1（是否被平台截断的长文）
  pics         [{pid, full, mid}]，full=最高画质 URL，mid=次级回退
"""
