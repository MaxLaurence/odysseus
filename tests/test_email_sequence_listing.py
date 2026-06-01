from routes.email_routes import _recent_uid_candidates_from_sequence


class FakeSequenceConn:
    def __init__(self, flags_by_seq=None, unseen_count=0):
        self.flags_by_seq = flags_by_seq or {}
        self.unseen_count = unseen_count
        self.fetch_calls = []
        self.uid_calls = []

    def fetch(self, seq_set, query):
        self.fetch_calls.append((seq_set, query))
        start_s, end_s = str(seq_set).split(":", 1)
        start, end = int(start_s), int(end_s)
        rows = []
        for seq in range(start, end + 1):
            flags = self.flags_by_seq.get(seq, "")
            rows.append(f"{seq} (UID {1000 + seq} FLAGS ({flags}))".encode())
        return "OK", rows

    def status(self, folder, query):
        return "OK", [f'{folder} (UNSEEN {self.unseen_count})'.encode()]

    def uid(self, *args):
        self.uid_calls.append(args)
        raise AssertionError("UID commands should not be needed for sequence-window listing")


def test_recent_uid_candidates_all_uses_sequence_window_without_uid_search():
    conn = FakeSequenceConn()

    uids, total = _recent_uid_candidates_from_sequence(
        conn,
        folder="INBOX",
        selected_count=6,
        limit=2,
        offset=1,
        filter_="all",
        has_attachments_only=False,
    )

    assert uids == [b"1005", b"1004"]
    assert total == 6
    assert conn.fetch_calls == [("4:5", "(UID FLAGS)")]
    assert conn.uid_calls == []


def test_recent_uid_candidates_unread_pages_from_newest_unseen_without_uid_search():
    conn = FakeSequenceConn(
        flags_by_seq={
            1: "\\Seen",
            2: "",
            3: "",
            4: "\\Seen",
            5: "",
            6: "\\Seen",
        },
        unseen_count=3,
    )

    uids, total = _recent_uid_candidates_from_sequence(
        conn,
        folder="INBOX",
        selected_count=6,
        limit=2,
        offset=1,
        filter_="unread",
        has_attachments_only=False,
    )

    assert uids == [b"1003", b"1002"]
    assert total == 3
    assert conn.uid_calls == []
