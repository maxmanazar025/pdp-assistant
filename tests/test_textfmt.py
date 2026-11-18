import re
from html.parser import HTMLParser

from memora.textfmt import TG_LIMIT, md_to_html, render_markdown, strip_tags

ALLOWED = {"b", "i", "u", "s", "code", "pre", "a"}


class _Balance(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.ok = True

    def handle_starttag(self, tag, attrs):
        self.ok &= tag in ALLOWED
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.ok = False


def assert_valid(html_text: str) -> None:
    checker = _Balance()
    checker.feed(html_text)
    assert checker.ok and not checker.stack, html_text[:200]
    assert len(html_text) <= TG_LIMIT


def test_code_block_is_escaped_and_tagged():
    out = md_to_html("Misol:\n```python\nif a < b and b > c:\n    print('<ok>')\n```\nTamom.")
    assert '<pre><code class="language-python">' in out
    assert "a &lt; b and b &gt; c" in out and "&lt;ok&gt;" in out
    assert_valid(out)


def test_inline_formatting():
    out = md_to_html("## Reja\n**Muhim:** `x<y` va *kursiv*\n- birinchi\n- ikkinchi")
    assert out.startswith("<b>Reja</b>")
    assert "<b>Muhim:</b>" in out and "<code>x&lt;y</code>" in out and "<i>kursiv</i>" in out
    assert "• birinchi" in out
    assert_valid(out)


def test_raw_html_from_model_cannot_break_parser():
    out = md_to_html("<script>alert(1)</script> <b>ok</b> <i>ochiq qoldi  5 < 7 && 9 > 2 <div>")
    assert "<script>" not in out and "<div>" not in out
    assert "<b>ok</b>" in out          # balanced whitelisted tag restored
    assert "&lt;i&gt;" in out          # unbalanced tag stays escaped
    assert_valid(out)


def test_stars_inside_code_are_untouched():
    out = md_to_html("`a * b * c` va ```\nx = **kwargs\n```")
    assert "<code>a * b * c</code>" in out and "**kwargs" in out
    assert_valid(out)


def test_unterminated_fence_is_closed():
    out = md_to_html("```js\nconst a = 1 < 2;")
    assert out.endswith("</code></pre>")
    assert_valid(out)


def test_long_answer_is_split_without_breaking_code():
    code = "\n".join(f"print({i})  # <{i}>" for i in range(700))
    text = "Kirish matni.\n\n" + "So'z " * 900 + f"\n\n```python\n{code}\n```\n\nXulosa."
    parts = render_markdown(text)
    assert len(parts) >= 3
    for part in parts:
        assert_valid(part)
    joined = strip_tags("\n".join(parts))
    assert "print(0)" in joined and "print(699)" in joined and "Xulosa." in joined
    assert len(re.findall(r"print\(\d+\)", joined)) == 700


def test_empty():
    assert render_markdown("   ") == []
