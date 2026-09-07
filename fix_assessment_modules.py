import pprint
import sys
from pathlib import Path


def main(path: str) -> None:
    src_path = Path(path)
    source = src_path.read_text(encoding="utf-8")

    ns: dict = {}
    exec(compile(source, str(src_path), "exec"), ns)

    training_modules = ns["GENERATED_TRAINING_MODULES"]
    assessment_modules = ns["GENERATED_ASSESSMENT_MODULES"]

    # Canonical question lookup from the training modules.
    canonical = {}
    for mod in training_modules:
        for q in mod["assessment"]:
            canonical[q["id"]] = q

    dropped = 0
    replaced = 0

    for asm in assessment_modules:
        fixed_questions = []

        for q in asm["questions"]:
            good = canonical.get(q["id"])

            if good is None:
                fixed_questions.append(q)
                continue

            # Remove questions whose only options are image placeholders.
            if good.get("options") == ["Image1", "Image2", "Image3", "Image4"]:
                dropped += 1
                continue

            # Replace only when the assessment copy differs from
            # its canonical training-module copy.
            if good != q:
                replaced += 1

            fixed_questions.append(good)

        asm["questions"] = fixed_questions

    print(f"Replaced {replaced} corrupted question(s).")
    print(f"Dropped {dropped} image-placeholder question(s).")

    # IMPORTANT:
    # Keep the original file structure and only replace the
    # GENERATED_ASSESSMENT_MODULES assignment.
    prefix, marker, remainder = source.partition(
        "GENERATED_ASSESSMENT_MODULES = "
    )

    if not marker:
        raise RuntimeError(
            "Could not find GENERATED_ASSESSMENT_MODULES in source file."
        )

    # Find the next top-level assignment after GENERATED_ASSESSMENT_MODULES.
    next_markers = [
        "\nGENERATED_COMPETENCIES_BY_ID = ",
        "\nGENERATED_PACKAGE_REQUIREMENTS = ",
    ]

    positions = [
        remainder.find(m)
        for m in next_markers
        if remainder.find(m) != -1
    ]

    if not positions:
        raise RuntimeError(
            "Could not locate the end of GENERATED_ASSESSMENT_MODULES."
        )

    end = min(positions)

    tail = remainder[end:]

    fixed_source = (
        prefix
        + marker
        + pprint.pformat(assessment_modules, width=100)
        + "\n\n"
        + tail.lstrip("\n")
    )

    out_path = src_path.with_name(src_path.stem + "_fixed.py")
    out_path.write_text(fixed_source, encoding="utf-8")

    print(f"Wrote fixed file to {out_path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python fix_assessment_modules.py path/to/seed_question_bank.py")
        sys.exit(1)

    main(sys.argv[1])