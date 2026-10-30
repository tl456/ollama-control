from datetime import datetime

def current_time():
    now = datetime.now().astimezone()
    offset = now.strftime("%z")
    return {"iso8601": now.isoformat(), "date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M:%S"), "timezone": "UTC" + offset[:3] + ":" + offset[3:]}
