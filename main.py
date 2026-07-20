#!/usr/bin/env python3
import argparse
import sys
import os
from kook_auth import login, login_with_token, KookSession
from kook_api import KookAPI
from kook_voice import VoiceClient


def cmd_login(args):
    if args.token:
        token_type = args.token_type or ("raw" if not args.token.startswith("Bot") else "bot")
        ks = login_with_token(args.token, token_type)
        print(f"Logged in: {ks.username} ({ks.user_id})")
        return ks

    if not args.phone:
        print("Error: --phone is required for user login")
        sys.exit(1)

    ks = login(args.phone, args.password,
               mobile_prefix=args.mobile_prefix or "86",
               endpoint=args.endpoint or None)
    print(f"Logged in: {ks.username} ({ks.user_id})")
    return ks


def cmd_list_guilds(api: KookAPI):
    guilds = api.get_guilds()
    print(f"\n{'ID':<25} {'Name':<20}")
    print("-" * 50)
    for g in guilds:
        print(f"{g['id']:<25} {g.get('name', 'N/A'):<20}")


def cmd_list_channels(api: KookAPI, args):
    channels = api.get_channels(args.guild, int(args.type))
    print(f"\n{'ID':<25} {'Name':<20} {'Type':<8}")
    print("-" * 60)
    for c in channels:
        t = "voice" if c.get("type") == 2 else "text"
        print(f"{c['id']:<25} {c.get('name', 'N/A'):<20} {t:<8}")


def cmd_list_voice(api: KookAPI):
    channels = api.list_voice_channels()
    print(f"\n{'ID':<25} {'Name':<20}")
    print("-" * 50)
    for c in channels:
        print(f"{c['id']:<25} {c.get('name', 'N/A'):<20}")


def _wait_loop(vc):
    print("Press Ctrl+C to leave")
    try:
        import time
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        vc.stop()


def cmd_join(api: KookAPI, args):
    vc = VoiceClient(api)
    info = vc.join(args.channel, args.password)
    print(f"\nJoined voice channel: {args.channel}")
    print(f"RTP: {info['ip']}:{info['port']}")
    print(f"Bitrate: {info.get('bitrate', 'N/A')}")

    if args.file:
        vc.push_file(args.file)
        print(f"Pushing audio file: {args.file}")
    else:
        vc.push_mic()
        print("麦克风通话中...")
    _wait_loop(vc)


def cmd_users(api: KookAPI, args):
    users = api.get_channel_users(args.channel)
    print(f"\nUsers in voice channel:")
    for u in users:
        print(f"  {u.get('username', 'N/A')} ({u.get('id', 'N/A')})")


def main():
    parser = argparse.ArgumentParser(description="KOOK Linux Voice Client")
    parser.add_argument("--session", help="Path to session file")
    parser.add_argument("--gui", action="store_true", help="Launch GUI")

    sub = parser.add_subparsers(dest="command")

    login_p = sub.add_parser("login", help="Login to KOOK")
    login_p.add_argument("--phone", help="Phone number")
    login_p.add_argument("--password", help="Password")
    login_p.add_argument("--mobile-prefix", default="86", help="Mobile prefix (default: 86)")
    login_p.add_argument("--token", help="Auth token (from web F12 capture)")
    login_p.add_argument("--token-type", choices=["raw", "bot", "bearer"], help="Token type (default: auto)")
    login_p.add_argument("--endpoint", help="Custom login API endpoint")

    guilds_p = sub.add_parser("guilds", help="List guilds")
    channels_p = sub.add_parser("channels", help="List channels")
    channels_p.add_argument("--guild", required=True, help="Guild ID")
    channels_p.add_argument("--type", default=2, type=int, help="1=text, 2=voice")

    voice_p = sub.add_parser("voice-list", help="List joined voice channels")

    join_p = sub.add_parser("join", help="Join a voice channel")
    join_p.add_argument("--channel", required=True, help="Channel ID")
    join_p.add_argument("--password", help="Channel password")
    join_p.add_argument("--file", help="Push audio file instead of mic")

    users_p = sub.add_parser("users", help="List users in a voice channel")
    users_p.add_argument("--channel", required=True, help="Channel ID")

    args = parser.parse_args()
    session_path = args.session or None

    if args.gui:
        from kook_gui import main as gui_main
        gui_main()
        return

    if args.command == "login":
        cmd_login(args)
        return

    ks = KookSession.load(session_path)
    if not ks:
        print("Not logged in. Run: python3 main.py login --phone <phone> --password <password>")
        sys.exit(1)

    api = KookAPI(ks)

    commands = {
        "guilds": lambda: cmd_list_guilds(api),
        "channels": lambda: cmd_list_channels(api, args),
        "voice-list": lambda: cmd_list_voice(api),
        "join": lambda: cmd_join(api, args),
        "users": lambda: cmd_users(api, args),
    }

    handler = commands.get(args.command)
    if handler:
        handler()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
