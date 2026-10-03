from agent_lite.core.tools.file_operations import text_diff


# 功能：展示用完整补丁不截断大型文件，末行无换行仍保留独立增删行
# 设计：工具摘要与 UI 共用生成器，验证默认限长及展示模式的不同边界
def test_display_diff_is_complete_and_separates_unterminated_lines() -> None:
    before = b"old\n" * 10_000 + b"last-old"
    after = b"new\n" * 10_000 + b"last-new"
    preview = text_diff(before, after, "file.txt")
    assert preview.endswith("[diff truncated]")
    complete = text_diff(before, after, "file.txt", max_chars=None)
    assert "[diff truncated]" not in complete
    assert "\n-last-old\n\\ No newline at end of file\n" in complete
    assert "\n+last-new\n\\ No newline at end of file\n" in complete
    lines = complete.splitlines()[3:]
    assert sum(line.startswith("+") for line in lines) == 10_001
    assert sum(line.startswith("-") for line in lines) == 10_001
