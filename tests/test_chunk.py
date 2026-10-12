from loops.ingest.chunk import Line, approx_tokens, split_paragraphs, window_lines
from tests.fakes import dt


def lines(n, start=1, text="hello there"):
    return [Line(i, dt(1, 10, i % 60), f"{text} {i}") for i in range(start, start + n)]


def test_windows_of_ten_with_overlap():
    drafts = window_lines(lines(25), max_messages=10, overlap=2)
    assert [d.message_ids[0] for d in drafts] == [1, 9, 17]
    assert drafts[0].message_ids == list(range(1, 11))
    assert drafts[1].message_ids == list(range(9, 19))
    assert drafts[-1].message_ids[-1] == 25


def test_every_message_lands_in_some_window():
    drafts = window_lines(lines(37), max_messages=10, overlap=2)
    covered = {i for d in drafts for i in d.message_ids}
    assert covered == set(range(1, 38))


def test_token_limit_splits_window():
    long = [Line(i, dt(1), "x" * 1200) for i in range(1, 5)]  # ~300 tokens each
    drafts = window_lines(long, max_tokens=800, overlap=1)
    assert all(sum(approx_tokens("x" * 1200) for _ in d.message_ids) <= 900 for d in drafts)
    assert {i for d in drafts for i in d.message_ids} == {1, 2, 3, 4}


def test_context_leads_first_window():
    ctx = lines(5, start=1)
    new = lines(3, start=6)
    drafts = window_lines(new, context=ctx, overlap=2)
    assert drafts[0].message_ids == [4, 5, 6, 7, 8]


def test_no_new_lines_no_chunks():
    assert window_lines([], context=lines(3)) == []


def test_split_paragraphs():
    short = "one\n\ntwo"
    assert split_paragraphs(short) == [short]
    body = "\n\n".join(["para " + "y" * 1000] * 6)
    pieces = split_paragraphs(body, max_tokens=600)
    assert len(pieces) > 1
    assert all(approx_tokens(p) <= 600 for p in pieces)
    huge = "z" * 10000
    assert all(approx_tokens(p) <= 800 for p in split_paragraphs(huge))
