# config.py（更新部分）

# ========== 多备用源配置（按优先级） ==========
BACKUP_SOURCES = [
    {
        "name": "酒店源 (B仓库)",
        "url": "https://gh-proxy.com/https://raw.githubusercontent.com/kingmax1688/iptv/refs/heads/main/itvlist.m3u",
        "priority": 1
    },
    {
        "name": "zilong7728/Collect聚合源",
        "url": "https://gh-proxy.com/https://raw.githubusercontent.com/zilong7728/Collect-IPTV/refs/heads/main/best_sorted.m3u",
        "priority": 2
    },
    {
        "name": "zbds综合源",
        "url": "https://live.zbds.top/tv/iptv4.m3u",
        "priority": 3
    },
    # 可以继续添加更多备用源，按需要调整 priority 数值
]

# 其他配置保持不变
CHECK_TIMEOUT = 3
MAX_WORKERS = 20
CHANNEL_ALIAS_MAP = {}
MIN_WIDTH = 1920
MIN_HEIGHT = 1080
MIN_BITRATE = 2000
ENABLE_QUALITY_CHECK = True
