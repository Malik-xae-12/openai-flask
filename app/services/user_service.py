def get_user_display_name(username: str) -> str:
    return username.strip() if username else "Guest"
