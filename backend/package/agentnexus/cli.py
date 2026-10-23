"""管理命令（decisions/0023）：创建第一个管理员、给升级前就存在的用户设置密码、建部门。

密码不从命令行参数读（会留在 shell 历史和进程列表里）：从环境变量（--password-env）或交互输入读取。
例：docker compose exec -e PW=... api python -m agentnexus.cli create-user --name admin --role admin --password-env PW
"""

import argparse
import asyncio
import getpass
import os
import sys

from sqlalchemy import select

from agentnexus.config import get_settings
from agentnexus.security.auth import AuthService
from agentnexus.storage.postgres import Department, User, make_engine, make_sessionmaker


def _password(args) -> str:
    if args.password_env:
        value = os.environ.get(args.password_env)
        if not value:
            sys.exit(f"environment variable {args.password_env} is empty")
        return value
    first, second = getpass.getpass("password: "), getpass.getpass("again: ")
    if first != second:
        sys.exit("passwords do not match")
    return first


async def _department_id(sm, name):
    if not name:
        return None
    async with sm() as s:
        dept = await s.scalar(select(Department).where(Department.name == name))
    if dept is None:
        sys.exit(f"department {name!r} not found (create it first)")
    return dept.id


async def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python -m agentnexus.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create-user")
    p.add_argument("--name", required=True)
    p.add_argument("--role", choices=["admin", "manager", "member"], default="member")
    p.add_argument("--department")
    p.add_argument("--password-env")
    p = sub.add_parser("set-password")
    p.add_argument("--name", required=True)
    p.add_argument("--password-env")
    p = sub.add_parser("create-department")
    p.add_argument("--name", required=True)
    args = parser.parse_args(argv)

    # users / departments / user_sessions 不受 RLS 约束：在 api 容器里用 api 角色执行也可以
    engine = make_engine(get_settings().database_url)
    sm = make_sessionmaker(engine)
    auth = AuthService(sm)
    try:
        if args.cmd == "create-department":
            print("created department", (await auth.create_department(args.name)).id)
        elif args.cmd == "create-user":
            user = await auth.create_user(args.name, _password(args), role=args.role,
                                          department_id=await _department_id(sm, args.department))
            print("created user", user.id)
        else:
            async with sm() as s:
                user = await s.scalar(select(User).where(User.name == args.name))
            if user is None:
                sys.exit(f"user {args.name!r} not found")
            await auth.update_user(user.id, password=_password(args))
            print("password updated")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
