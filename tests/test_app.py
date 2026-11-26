import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from sqlalchemy import func, select

from studyroom import create_app
from studyroom.booking import TIMEZONE, slot_taken
from studyroom.models import Booking, OccupiedSlot, db_session

PASSWORD = "StudyRoom-Demo26!"
TODAY = "2030-04-10"
TOMORROW = "2030-04-11"


class StudyRoomTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        now = datetime(2030, 4, 10, 8, 3, tzinfo=TIMEZONE)
        frozen = patch("studyroom.booking.local_now", return_value=now)
        frozen.start()
        self.addCleanup(frozen.stop)
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-only-key-not-for-deployment-x",
            "DATABASE_BACKEND": "sqlite",
            "SESSION_COOKIE_SECURE": False,
            "SQLITE_PATH": Path(folder.name) / "test.sqlite",
        })
        self.addCleanup(self.app.extensions["studyroom_engine"].dispose)
        with patch("studyroom.commands.local_now", return_value=now):
            for args in (["init-db"], ["init-db"], ["seed-demo"], ["seed-demo"]):
                result = self.app.test_cli_runner().invoke(args=args)
                self.assertEqual(result.exit_code, 0, result.output)
        self.client = self.app.test_client()

    def token(self, client, path="/"):
        page = client.get(path)
        return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)

    def login(self, client, password=PASSWORD):
        return client.post("/dang-nhap", data={
            "csrf": self.token(client, "/dang-nhap"), "username": "demo", "password": password,
        })

    def register(self, client, username="student2", name="Nguyễn Văn An"):
        return client.post("/dang-ky", data={
            "csrf": self.token(client, "/dang-ky"), "username": username, "name": name,
            "password": "GroupTest!1234", "confirm": "GroupTest!1234",
        })

    def book(self, client, room_id=1, **fields):
        return client.post(f"/phong/{room_id}/dat", data={
            "csrf": self.token(client), "day": TOMORROW, "start": 630, "end": 690,
            "group_size": 3, "purpose": "Chuẩn bị phần trình bày",
        } | fields)

    def count(self, model):
        with self.app.app_context(), db_session() as db:
            return db.scalar(select(func.count()).select_from(model))

    def test_auth_and_csrf(self):
        self.assertEqual(self.client.get("/").location, "/dang-nhap")
        self.assertEqual(self.login(self.client, password="incorrect").status_code, 401)
        login = self.login(self.client)
        self.assertEqual(login.status_code, 303)
        self.assertIn("HttpOnly", login.headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", login.headers["Set-Cookie"])
        self.assertEqual(self.book(self.client, csrf="wrong").status_code, 400)

    def test_register(self):
        self.assertEqual(self.register(self.client, username="!").status_code, 422)
        self.assertEqual(self.register(self.client).status_code, 303)
        self.assertIn("Nguyễn Văn An", self.client.get("/").text)
        self.assertEqual(self.register(self.app.test_client(), username="STUDENT2").status_code, 409)

    def test_capacity_and_schedule(self):
        self.login(self.client)
        html = self.client.get(f"/?day={TOMORROW}&seats=7").text
        self.assertIn("B-302", html)
        self.assertIn("C-105", html)
        self.assertNotIn("A-201", html)
        html = self.client.get(f"/?day={TOMORROW}").text
        self.assertIn("4 phòng phù hợp", html)
        self.assertIn('class="slot busy"', html)
        self.assertNotIn("Ôn thi cơ sở dữ liệu", html)
        self.assertEqual(self.count(Booking), 3)

    def test_overlap_and_adjacent_times(self):
        self.login(self.client)
        self.assertEqual(self.book(self.client, start=570, end=660).status_code, 409)
        self.assertEqual(self.count(Booking), 3)
        self.assertEqual(self.book(self.client, start=630, end=690).status_code, 303)
        self.assertEqual(self.book(self.client, start=660, end=720).status_code, 409)
        self.assertEqual(self.book(self.client, start=690, end=720).status_code, 303)
        self.assertEqual(self.book(self.client, room_id=2, start=660, end=720).status_code, 303)

    def test_time_capacity_and_length_validation(self):
        self.login(self.client)
        for invalid in (
            {"day": TODAY, "start": 480, "end": 510}, {"day": "2030-05-01"},
            {"day": "not-a-date"}, {"start": 631}, {"end": 810},
            {"start": 1230, "end": 1290}, {"start": 660, "end": 630},
            {"group_size": 5}, {"purpose": "🔧" * 71},
        ):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.book(self.client, **invalid).status_code, 422)
        self.assertEqual(self.count(Booking), 3)

    def test_cancel_owner_only_and_release_slots(self):
        self.login(self.client)
        self.book(self.client)
        own_list = self.client.get("/lich-cua-toi").text
        action = re.search(r'action="(/lich/\d+/huy)"[^>]*>', own_list)
        self.assertIn("Chuẩn bị phần trình bày", own_list)
        other = self.app.test_client()
        self.register(other)
        self.assertEqual(other.post(action.group(1), data={"csrf": self.token(other)}).status_code, 404)
        self.assertEqual(self.book(other).status_code, 409)
        for _ in range(2):
            self.assertEqual(self.client.post(action.group(1), data={"csrf": self.token(self.client)}).status_code, 303)
        self.assertIn("Đã hủy", self.client.get("/lich-cua-toi?view=history").text)
        self.assertEqual(self.book(other).status_code, 303)

    def test_cannot_cancel_after_start(self):
        self.login(self.client)
        self.book(self.client)
        path = re.search(r'action="(/lich/\d+/huy)"', self.client.get("/lich-cua-toi").text).group(1)
        csrf = self.token(self.client)
        with patch("studyroom.booking.local_now", return_value=datetime(2030, 4, 11, 10, 30, tzinfo=TIMEZONE)):
            self.assertEqual(self.client.post(path, data={"csrf": csrf}).status_code, 409)

    def test_two_sessions_race_for_same_room(self):
        clients = [self.client, self.app.test_client()]
        self.login(clients[0])
        self.register(clients[1])
        barrier = Barrier(2)
        seen = set()

        def synchronize_first_check(db, *args):
            busy = slot_taken(db, *args)
            if db not in seen:
                seen.add(db)
                barrier.wait(timeout=5)
            return busy

        with patch("studyroom.booking.slot_taken", side_effect=synchronize_first_check):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(self.book, clients))
        self.assertEqual(sorted(result.status_code for result in results), [303, 409])
        self.assertEqual(self.count(Booking), 4)
        self.assertEqual(self.count(OccupiedSlot), 11)


if __name__ == "__main__":
    unittest.main()
