"""交互式创建管理员：不提供默认密码，也不输出密码或哈希。"""

import argparse
from getpass import getpass

from pydantic import ValidationError

from app.database import SessionLocal, init_database
from app.models.schemas import UserRegisterRequest
from app.services.auth_service import UsernameConflictError, create_user


def main() -> None:
    parser = argparse.ArgumentParser(description="创建本地管理员账号")
    parser.add_argument("username", help="3–32 位字母、数字或下划线")
    args = parser.parse_args()
    password = getpass("管理员密码（8–128 位）: ")
    confirmation = getpass("再次输入密码: ")
    if password != confirmation:
        raise SystemExit("两次密码不一致，未创建管理员。")
    try:
        request = UserRegisterRequest(username=args.username, password=password)
    except ValidationError as exc:
        message = exc.errors(include_url=False)[0]["msg"]
        raise SystemExit(f"输入不合法：{message}") from None

    init_database()
    try:
        with SessionLocal() as session:
            user = create_user(
                session, request.username, request.password.get_secret_value(), is_admin=True,
            )
    except UsernameConflictError as exc:
        raise SystemExit(str(exc)) from None
    print(f"管理员已创建：id={user.id}，username={user.username}")


if __name__ == "__main__":
    main()
