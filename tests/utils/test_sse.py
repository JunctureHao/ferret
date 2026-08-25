"""SSE 事件流解析。纯函数，没有 Qt、没有 mitmproxy。

规范（WHATWG HTML「Interpreting an event stream」）里有一批规则**看着像细节，错了却
会静默丢数据**，这里逐条钉住：

* 多条 `data:` 按换行拼接，不是覆盖 —— 丢了就只剩最后一行；
* 值前面恰好去掉一个空格，不是 `lstrip()` —— 缩进过的 JSON 会被啃掉；
* `\\r\\n` / `\\r` / `\\n` 三种分隔符都算 —— 只认 `\\n` 的话，CRLF 流量每行尾巴挂个
  `\\r`，`retry` 之类的数字字段跟着失效；
* 末尾没有空行时最后一段照样派发 —— 这里拿到的是收完的 body，不是还在流的连接。

刻意偏离规范的那一条（注释块也产出事件）单独一组钉住，免得后来人「照规范修正」把
心跳流又改回空表。
"""

import unittest

from ferret.utils.sse import (
    DEFAULT_EVENT,
    SSE_CONTENT_TYPE,
    SseEvent,
    is_event_stream,
    parse_sse,
)


class ContentTypeTests(unittest.TestCase):
    def test_the_bare_type_matches(self) -> None:
        self.assertTrue(is_event_stream(SSE_CONTENT_TYPE))

    def test_parameters_and_casing_do_not_matter(self) -> None:
        for value in (
            "text/event-stream; charset=utf-8",
            "Text/Event-Stream",
            "  text/event-stream  ",
            "text/event-stream;charset=UTF-8",
        ):
            with self.subTest(value=value):
                self.assertTrue(is_event_stream(value))

    def test_other_types_do_not_match(self) -> None:
        for value in ("application/json", "text/plain", ""):
            with self.subTest(value=value):
                self.assertFalse(is_event_stream(value))

    def test_a_longer_type_name_is_not_a_match(self) -> None:
        """`startswith` 单独用会把它认成事件流，然后拿 SSE 规则解一份不是 SSE 的 body。"""
        for value in (
            "text/event-streamx",
            "text/event-stream-v2",
            "text/event-streams",
        ):
            with self.subTest(value=value):
                self.assertFalse(is_event_stream(value))

    def test_a_missing_header_is_not_an_error(self) -> None:
        """详情字典里这一格缺失时是 ``"-"``，也可能压根没有键。"""
        for value in (None, "-", 0, object()):
            with self.subTest(value=value):
                self.assertFalse(is_event_stream(value))


class ParseTests(unittest.TestCase):
    def test_an_empty_stream_has_no_events(self) -> None:
        self.assertEqual(parse_sse(""), [])
        self.assertEqual(parse_sse("\n\n\n"), [])

    def test_a_single_block_becomes_one_event(self) -> None:
        events = parse_sse("event: price\ndata: 42\nid: 7\nretry: 3000\n\n")

        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0],
            SseEvent(
                index=0,
                event="price",
                data="42",
                id="7",
                retry=3000,
                comment="",
                raw="event: price\ndata: 42\nid: 7\nretry: 3000",
            ),
        )

    def test_multiple_data_lines_are_joined_with_newlines(self) -> None:
        """覆盖而不是拼接是最常见的写法错误，而结果是「只显示最后一行」。"""
        events = parse_sse("data: line one\ndata: line two\ndata: line three\n\n")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].data, "line one\nline two\nline three")

    def test_an_empty_data_line_still_counts(self) -> None:
        """`data:` 单独一行是合法的空消息，不是「没有 data」。"""
        events = parse_sse("data:\n\n")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].data, "")
        self.assertEqual(events[0].event, DEFAULT_EVENT)

    def test_data_lines_join_even_when_some_are_empty(self) -> None:
        events = parse_sse("data: a\ndata:\ndata: b\n\n")
        self.assertEqual(events[0].data, "a\n\nb")

    def test_exactly_one_leading_space_is_dropped(self) -> None:
        """`lstrip()` 会把缩进过的 JSON 啃掉，规范只让去掉一个空格。"""
        self.assertEqual(parse_sse("data:  indented\n\n")[0].data, " indented")
        self.assertEqual(parse_sse("data:no space\n\n")[0].data, "no space")
        self.assertEqual(parse_sse("data:\ttab\n\n")[0].data, "\ttab")

    def test_a_field_without_a_colon_is_a_name_with_an_empty_value(self) -> None:
        """`data` 光秃秃一行等价于 `data:`。"""
        events = parse_sse("data\n\n")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].data, "")
        self.assertEqual(events[0].event, DEFAULT_EVENT)

    def test_events_are_numbered_in_arrival_order(self) -> None:
        events = parse_sse("data: a\n\ndata: b\n\ndata: c\n\n")

        self.assertEqual([e.index for e in events], [0, 1, 2])
        self.assertEqual([e.data for e in events], ["a", "b", "c"])

    def test_a_block_without_an_event_field_gets_the_spec_default(self) -> None:
        self.assertEqual(parse_sse("data: hi\n\n")[0].event, DEFAULT_EVENT)

    def test_an_explicit_event_name_wins(self) -> None:
        self.assertEqual(parse_sse("event: tick\ndata: hi\n\n")[0].event, "tick")

    def test_blank_lines_between_blocks_do_not_produce_empty_events(self) -> None:
        """心跳只发 `\\n\\n` 的服务端不少，一串空行不该变成一串空行事件。"""
        events = parse_sse("data: a\n\n\n\n\ndata: b\n\n")

        self.assertEqual([e.data for e in events], ["a", "b"])


class SeparatorTests(unittest.TestCase):
    def test_crlf_is_a_line_separator(self) -> None:
        events = parse_sse("event: tick\r\ndata: 42\r\nretry: 100\r\n\r\n")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event, "tick")
        self.assertEqual(events[0].data, "42")
        # `\r` 漏进值里的话这里会是 None —— `"100\r".isdigit()` 是假。
        self.assertEqual(events[0].retry, 100)

    def test_a_bare_cr_is_a_line_separator(self) -> None:
        events = parse_sse("data: a\rdata: b\r\r")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].data, "a\nb")

    def test_mixed_separators_all_work(self) -> None:
        events = parse_sse("data: a\r\ndata: b\rdata: c\n\n")
        self.assertEqual(events[0].data, "a\nb\nc")

    def test_raw_normalises_separators_to_newlines(self) -> None:
        self.assertEqual(
            parse_sse("data: a\r\ndata: b\r\n\r\n")[0].raw, "data: a\ndata: b"
        )

    def test_the_exotic_unicode_breaks_are_data_not_separators(self) -> None:
        """`str.splitlines()` 会在这些字符上断行，而 SSE 里它们是数据的一部分。"""
        for char in ("\x0b", "\x0c", "\x1c", " ", ""):
            with self.subTest(char=repr(char)):
                events = parse_sse(f"data: a{char}b\n\n")
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].data, f"a{char}b")


class UnterminatedTests(unittest.TestCase):
    def test_a_trailing_block_without_a_blank_line_is_still_dispatched(self) -> None:
        """规范面对的是还在流动的连接；这里的 body 已经收完了，不派发就是凭空少一条。"""
        events = parse_sse("data: first\n\ndata: last")

        self.assertEqual([e.data for e in events], ["first", "last"])

    def test_a_single_unterminated_block_is_the_only_event(self) -> None:
        events = parse_sse("event: done\ndata: bye")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event, "done")
        self.assertEqual(events[0].data, "bye")


class FieldTests(unittest.TestCase):
    def test_unknown_fields_are_ignored(self) -> None:
        """规范：不认识的字段名整行忽略，不是报错、也不是塞进 data。"""
        events = parse_sse("data: hi\nchannel: btc\nfoo\n\n")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].data, "hi")
        # 但原文里还看得见 —— 排查时那一行可能正是关键。
        self.assertIn("channel: btc", events[0].raw)

    def test_retry_takes_ascii_digits_only(self) -> None:
        self.assertEqual(parse_sse("retry: 5000\ndata: x\n\n")[0].retry, 5000)
        for value in ("abc", "1.5", "-1", "1 000", ""):
            with self.subTest(value=value):
                self.assertIsNone(parse_sse(f"retry: {value}\ndata: x\n\n")[0].retry)

    def test_full_width_digits_are_not_ascii_digits(self) -> None:
        """`"１２３".isdigit()` 是真而 `int()` 照样吃 —— 会把乱码当成重连间隔。"""
        self.assertIsNone(parse_sse("retry: １２３\ndata: x\n\n")[0].retry)

    def test_an_id_containing_nul_is_ignored_entirely(self) -> None:
        """规范说忽略整条，不是截断。"""
        self.assertEqual(parse_sse("id: a\x00b\ndata: x\n\n")[0].id, "")

    def test_the_last_value_of_a_repeated_scalar_field_wins(self) -> None:
        events = parse_sse("event: a\nevent: b\nid: 1\nid: 2\ndata: x\n\n")

        self.assertEqual(events[0].event, "b")
        self.assertEqual(events[0].id, "2")

    def test_each_block_reports_its_own_id_not_an_inherited_one(self) -> None:
        """表格那一列要回答「哪些事件带了 id」，继承来的值填进去反而看不出。"""
        events = parse_sse("id: 7\ndata: a\n\ndata: b\n\n")

        self.assertEqual(events[0].id, "7")
        self.assertEqual(events[1].id, "")


class CommentTests(unittest.TestCase):
    """刻意偏离规范的那一条：注释块也产出事件。"""

    def test_a_heartbeat_block_becomes_an_event(self) -> None:
        """按规范解完一份心跳流会得到空列表 —— 界面上看着像什么都没抓到。"""
        events = parse_sse(": keep-alive\n\n: keep-alive\n\n")

        self.assertEqual(len(events), 2)
        self.assertEqual([e.comment for e in events], ["keep-alive", "keep-alive"])

    def test_a_heartbeat_has_no_event_type(self) -> None:
        """规范根本不派发这种块，硬填一个 `message` 是编数据。"""
        event = parse_sse(": ping\n\n")[0]

        self.assertEqual(event.event, "")
        self.assertEqual(event.data, "")

    def test_a_bare_colon_is_an_empty_comment_not_a_missing_one(self) -> None:
        events = parse_sse(":\n\n")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].comment, "")
        self.assertEqual(events[0].raw, ":")

    def test_comments_riding_along_with_data_are_kept_separately(self) -> None:
        events = parse_sse(": served by node-3\ndata: 42\n\n")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].comment, "served by node-3")
        self.assertEqual(events[0].data, "42")
        self.assertEqual(events[0].event, DEFAULT_EVENT)

    def test_multiple_comment_lines_join(self) -> None:
        self.assertEqual(parse_sse(": a\n: b\n\n")[0].comment, "a\nb")


class ValueObjectTests(unittest.TestCase):
    def test_an_event_is_frozen(self) -> None:
        """界面拿着它填表，改不动才谈得上「这一条当时是什么」。"""
        event = parse_sse("data: x\n\n")[0]
        with self.assertRaises(AttributeError):
            event.data = "y"  # type: ignore

    def test_events_compare_by_value(self) -> None:
        self.assertEqual(parse_sse("data: x\n\n"), parse_sse("data: x\n\n"))


class RealisticStreamTests(unittest.TestCase):
    def test_an_openai_style_stream_parses_end_to_end(self) -> None:
        """真实形状：分块 JSON + 心跳 + `[DONE]` 哨兵，末尾还少一个空行。"""
        body = (
            ": ok\r\n"
            "\r\n"
            'data: {"choices":[{"delta":{"content":"He"}}]}\r\n'
            "\r\n"
            'data: {"choices":[{"delta":{"content":"llo"}}]}\r\n'
            "\r\n"
            "retry: 15000\r\n"
            "\r\n"
            "data: [DONE]"
        )
        events = parse_sse(body)

        self.assertEqual(len(events), 5)
        self.assertEqual(events[0].comment, "ok")
        self.assertEqual(events[0].event, "")
        self.assertTrue(events[1].data.startswith('{"choices"'))
        self.assertEqual(events[1].event, DEFAULT_EVENT)
        self.assertTrue(events[2].data.endswith('"llo"}}]}'))
        self.assertEqual(events[3].retry, 15000)
        self.assertEqual(events[3].event, "")
        self.assertEqual(events[4].data, "[DONE]")
        self.assertEqual([e.index for e in events], [0, 1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()
