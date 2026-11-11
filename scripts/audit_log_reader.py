"""감사 로그 읽기 헬퍼 (issue #177). 표준 라이브러리만 쓴다.

`audit/gateway.log`·`audit/recall-queries.log` 는 `rerank-server/rotate-logs.sh` 가 `<path>.1.gz ... <path>.N.gz` 로 회전한다.
리더가 라이브 파일만 읽으면 창 앞쪽 줄이 조용히 빠진다. 이 모듈은 `path.N.gz ... path.1.gz, path` 를 오래된 순으로 잇고,
colima virtiofs 에서 호스트 `: >` 회전 뒤 첫 줄 앞에 생기는 NUL 구멍을 줄 앞에서 걷어낸 줄 목록을 돌려준다.
손상된(잘렸거나 깨진) .gz 는 읽히는 앞부분만 쓰고 사유를 `damaged` 에 남긴다(bench/canary.py `_read_gz_prefix` 와 같은 원칙).

소비자: model_audit.py, duplicate_recall.py, tag_hygiene.py(report 경로에서만 지연 import). 각 스크립트는 import 전에 자기
디렉터리를 sys.path 에 넣는다 - scripts/ 는 패키지가 아니다.
"""
from __future__ import annotations

import gzip
import io
import os
import re
import zlib
from typing import Any, Dict, Iterable, List, Optional, Union


class LogRead:
    """읽기 결과. `lines` 는 NUL 앞머리를 걷은 비어 있지 않은 줄(개행 제외)이다."""

    def __init__(self) -> None:
        self.files: List[str] = []
        self.lines: List[str] = []
        self.rotated = 0               # 읽은 .gz 세대 수(경로로 직접 준 .gz 포함)
        self.nul_bytes = 0             # 줄 앞에서 걷어낸 NUL 바이트 수
        self.damaged: List[str] = []   # "<파일명>: <사유>"

    @property
    def extras(self) -> bool:
        """회전 세대·NUL·손상 중 하나라도 있었나. 아니면 출력에 새 키를 넣지 않는다(기존 리포트 재현성)."""
        return bool(self.rotated or self.nul_bytes or self.damaged)

    def info(self) -> Dict[str, Any]:
        return {"files": [os.path.basename(f) for f in self.files], "rotated_generations": self.rotated,
                "nul_bytes": self.nul_bytes, "damaged": list(self.damaged)}


def generation_files(path: str) -> List[str]:
    """path 의 읽을 파일들을 오래된 순으로: path.N.gz ... path.1.gz, (있으면) path.1, path. path 가 .gz 면 그 파일 하나만(이미
    회전본을 직접 가리킨 것). path.1 은 rotate-logs.sh 가 `cp path path.1 && : > path` 한 뒤 `gzip -f path.1` 하기 전(또는 gzip 이
    실패해 남은) 비압축 최신 회전본이다. 방금 회전한 줄이라 .1.gz 보다 새롭고 라이브보다 오래됐다."""
    if path.endswith(".gz"):
        return [path]
    directory = os.path.dirname(path) or "."
    pattern = re.compile(r"^" + re.escape(os.path.basename(path)) + r"\.(\d+)\.gz$")
    found = []
    try:
        names = os.listdir(directory)
    except OSError:
        names = []
    for name in names:
        match = pattern.match(name)
        if match:
            found.append((int(match.group(1)), os.path.join(directory, name)))
    found.sort(reverse=True)           # N 이 클수록 오래된 세대
    ordered = [p for _, p in found]
    if os.path.exists(path + ".1"):
        ordered.append(path + ".1")
    return ordered + [path]


def _gz_prefix(path: str):
    """(바이트, 손상 사유 또는 None). 끝이 잘렸거나(EOFError) 깨졌으면(BadGzipFile·zlib.error) 그 앞까지 풀린 바이트만 쓰고
    반쯤 풀린 마지막 줄은 버린다. 0 바이트 .gz 도 손상이다(빈 입력을 gzip 해도 헤더·꼬리가 있다)."""
    chunks: List[bytes] = []
    problem: Optional[str] = None
    try:
        if os.path.getsize(path) == 0:
            problem = "empty file"
        else:
            with gzip.open(path, "rb") as fh:
                while True:            # read1 로 조각씩 받아 예외 직전까지 풀린 바이트를 잃지 않는다
                    chunk = fh.read1(1 << 16)
                    if not chunk:
                        break
                    chunks.append(chunk)
    except (EOFError, gzip.BadGzipFile, zlib.error) as exc:
        problem = type(exc).__name__
    data = b"".join(chunks)
    if problem is not None:
        data = data[:data.rfind(b"\n") + 1]
    return data, problem


def read_log(paths: Union[str, "os.PathLike[str]", Iterable[Union[str, "os.PathLike[str]"]]],
             errors: str = "strict") -> LogRead:
    """경로(또는 경로 여러 개)의 회전 세대를 포함해 읽는다. 같은 파일은 한 번만 읽는다(라이브와 .1.gz 를 함께 줘도 중복 없음).
    라이브 파일이 없으면 FileNotFoundError 가 그대로 난다. 디코딩 errors 기본값은 "strict" 다(예전 open(..., encoding="utf-8") 과 같다:
    깨진 바이트가 있으면 UnicodeDecodeError). 예전에 errors="replace" 로 읽던 호출자(model_audit)만 "replace" 를 넘긴다."""
    result = LogRead()
    seen = set()
    for path in ([paths] if isinstance(paths, (str, os.PathLike)) else list(paths)):
        given = os.fspath(path)
        for name in generation_files(given):
            key = os.path.realpath(name)
            if key in seen:
                continue
            seen.add(key)
            try:
                if name.endswith(".gz"):
                    data, problem = _gz_prefix(name)
                else:
                    with open(name, "rb") as fh:
                        data, problem = fh.read(), None
            except FileNotFoundError:
                if name == given:
                    raise                   # 호출자가 직접 가리킨 파일은 없으면 예전처럼 오류다
                # 세대 목록을 만든 뒤 회전(rotate-logs.sh mv/gzip)이 그 파일을 옮겼다: 건너뛰고 손상과 같은 방식으로 남긴다
                result.damaged.append(f"{os.path.basename(name)}: FileNotFoundError - vanished after listing "
                                      "(rotation in progress), skipped")
                continue
            if name != given or name.endswith(".gz"):
                result.rotated += 1         # 회전 세대(.N.gz·.1, 직접 준 .gz 포함)
            if problem is not None:
                result.damaged.append(f"{os.path.basename(name)}: {problem} - only its readable first part was used")
            result.files.append(name)
            for raw in io.TextIOWrapper(io.BytesIO(data), encoding="utf-8", errors=errors, newline="\n"):
                line = raw.rstrip("\n")
                clean = line.lstrip("\x00")
                result.nul_bytes += len(line) - len(clean)
                if clean.strip():
                    result.lines.append(clean)
    return result
