"""Exercise the actual capture report's path expression without a browser."""
import ast
from pathlib import Path
from types import SimpleNamespace
import pytest


def source_video_expression():
    source = Path(__file__).resolve().parents[2] / 'scripts/capture_guided_demo.py'
    tree = ast.parse(source.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == 'source_video':
                    return compile(ast.Expression(value), str(source), 'eval')
    raise AssertionError('capture report lost its original video reference')


@pytest.mark.parametrize('relative', [True, False])
def test_absolute_browser_video_is_relative_to_cli_output(tmp_path, monkeypatch, relative):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / 'verification' / 'media'
    video_file = root / 'raw' / 'page.webm'
    video_file.parent.mkdir(parents=True)
    video_file.touch()
    output = Path('verification/media') if relative else root
    # Playwright returns an absolute path even when record_video_dir is relative.
    video = SimpleNamespace(path=lambda: str(video_file))
    actual = eval(source_video_expression(), {'Path': Path, 'video': video, 'output': output})
    assert actual == 'raw/page.webm'
    assert (output / actual).resolve() == video_file


def test_capture_must_not_reference_video_outside_output(tmp_path):
    video = SimpleNamespace(path=lambda: str(tmp_path / 'other.webm'))
    with pytest.raises(ValueError):
        eval(source_video_expression(), {'Path': Path, 'video': video, 'output': tmp_path / 'media'})
