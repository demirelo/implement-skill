import pytest

from implement_skill.execute import DispatchError, make_ow_dispatcher


PATCH = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"


def dispatch_response(response):
    def runner(_argv, **_kwargs):
        class Result:
            returncode = 0
            stdout = response
            stderr = ""

        return Result()

    return make_ow_dispatcher("deepseek", runner=runner)("request")


def test_language_example_before_patch_does_not_hide_applicable_diff():
    response = "```typescript\nconst x = 1;\n```\n\n```diff\n" + PATCH + "```\n"
    assert dispatch_response(response) == PATCH


@pytest.mark.parametrize("label", ["diff", "patch", "DIFF", ""])
def test_fenced_patch_preserves_git_metadata_and_crlf_wrapper(label):
    patch = "diff --git a/x b/x\nold mode 100644\nnew mode 100755\n" + PATCH
    assert dispatch_response(f"```{label}\r\n{patch}```\r\n") == patch


def test_bare_diff_is_preserved():
    assert dispatch_response(PATCH) == PATCH


def test_multiple_alternative_patch_fences_fail_instead_of_silently_dropping_one():
    response = "```diff\n" + PATCH + "```\n```patch\n" + PATCH + "```\n"
    with pytest.raises(DispatchError, match="ambiguous"):
        dispatch_response(response)
