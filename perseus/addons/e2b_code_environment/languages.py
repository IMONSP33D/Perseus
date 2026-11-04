"""The languages run_code offers: one entry each, independent of which backend runs them.

A backend only supplies a Linux machine with the toolchains installed (see
setup/sandbox/Dockerfile). At broker start-up each entry's `check` command runs
once in a real sandbox; only languages whose check succeeds are offered to the
model, so a missing toolchain never shows up as a choice.

Adding a language is one entry here plus a line in the sandbox image.
Commands run with the job folder as the working directory and must not need
the network (sandboxes have none by default).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    name: str                 # the enum value the model sends
    label: str                # shown in the dashboard and in results
    file: str                 # the source file the code is written to
    run: str                  # how to run it (after a successful build, if any)
    check: str                # exits 0 when the toolchain is installed
    build: str | None = None  # compile step, if the language has one


LANGUAGES: dict[str, Language] = {lang.name: lang for lang in (
    Language("python", "Python 3", "main.py", run="python3 main.py", check="python3 --version"),
    Language("javascript", "JavaScript (Node.js)", "main.js", run="node main.js", check="node --version"),
    Language("bash", "Bash", "main.sh", run="bash main.sh", check="bash --version"),
    Language("c", "C (gcc, C17)", "main.c", build="gcc -O2 -Wall -std=c17 main.c -o main -lm",
             run="./main", check="gcc --version"),
    Language("cpp", "C++ (g++, C++20)", "main.cpp", build="g++ -O2 -Wall -std=c++20 main.cpp -o main",
             run="./main", check="g++ --version"),
    Language("rust", "Rust (rustc, 2021 edition)", "main.rs", build="rustc -O --edition 2021 main.rs -o main",
             run="./main", check="rustc --version"),
    Language("go", "Go", "main.go", build='GOCACHE="$PWD/.gocache" GOPATH="$PWD/.gopath" go build -o main main.go',
             run="./main", check="go version"),
    Language("java", "Java (single file)", "Main.java", run="java Main.java", check="java -version"),
)}


def names() -> list[str]:
    return list(LANGUAGES)
