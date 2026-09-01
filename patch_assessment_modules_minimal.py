import ast
import re
import sys
from pathlib import Path


def find_assignment(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return node
    raise RuntimeError(f"Could not find {name}")


def dict_field(node, field_name):
    if not isinstance(node, ast.Dict):
        return None

    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and key.value == field_name:
            return value

    return None


def string_value(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def question_nodes(assignment):
    result = {}

    for node in ast.walk(assignment.value):
        if not isinstance(node, ast.Dict):
            continue

        id_node = dict_field(node, "id")
        qid = string_value(id_node)

        if qid:
            result.setdefault(qid, []).append(node)

    return result


def source_offset_table(source):
    data = source.encode("utf-8")
    starts = [0]

    for i, b in enumerate(data):
        if b == 10:
            starts.append(i + 1)

    return data, starts


def node_span(node, starts):
    start = starts[node.lineno - 1] + node.col_offset
    end = starts[node.end_lineno - 1] + node.end_col_offset
    return start, end


def main(path):
    src_path = Path(path)
    source = src_path.read_text(encoding="utf-8")

    tree = ast.parse(source, filename=str(src_path))

    training = find_assignment(tree, "GENERATED_TRAINING_MODULES")
    assessments = find_assignment(tree, "GENERATED_ASSESSMENT_MODULES")

    # Read the original data to determine which assessment copies
    # actually differ from their canonical training copies.
    ns = {}
    exec(compile(source, str(src_path), "exec"), ns)

    canonical_values = {}

    for mod in ns["GENERATED_TRAINING_MODULES"]:
        for q in mod["assessment"]:
            canonical_values[q["id"]] = q

    canonical_nodes = question_nodes(training)
    assessment_nodes = question_nodes(assessments)

    replacements = []
    replaced = 0
    dropped = 0

    data, starts = source_offset_table(source)

    for qid, nodes in assessment_nodes.items():
        good = canonical_values.get(qid)
        good_nodes = canonical_nodes.get(qid)

        if good is None or not good_nodes:
            continue

        canonical_start, canonical_end = node_span(
            good_nodes[0], starts
        )

        canonical_text = data[
            canonical_start:canonical_end
        ]

        for node in nodes:

            # Remove image-placeholder questions.
            if good.get("options") == [
                "Image1",
                "Image2",
                "Image3",
                "Image4",
            ]:
                start, end = node_span(node, starts)

                after = data[end:]

                comma_match = re.match(
                    rb"[ \t]*,[ \t]*(?:\r?\n)?",
                    after,
                )

                if comma_match:
                    end += comma_match.end()
                else:
                    before = data[:start]

                    comma_match = re.search(
                        rb",[ \t]*(?:\r?\n)?$",
                        before,
                    )

                    if comma_match:
                        start -= comma_match.end()

                replacements.append((start, end, b""))
                dropped += 1

            else:
                current_value = ast.literal_eval(node)

                # Replace only genuinely different assessment questions.
                if current_value != good:
                    start, end = node_span(node, starts)

                    replacements.append(
                        (
                            start,
                            end,
                            canonical_text,
                        )
                    )

                    replaced += 1

    # Apply replacements from bottom to top.
    for start, end, replacement in sorted(
        replacements,
        reverse=True,
    ):
        data = (
            data[:start]
            + replacement
            + data[end:]
        )

    out_path = src_path.with_name(
        src_path.stem + "_minimal_fixed.py"
    )

    out_path.write_bytes(data)

    print(f"Replaced {replaced} corrupted question(s).")
    print(f"Dropped {dropped} image-placeholder question(s).")
    print(f"Wrote minimal fixed file to {out_path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(
            "Usage: python patch_assessment_modules_minimal.py "
            "path/to/seed_question_bank.py"
        )
        sys.exit(1)

    main(sys.argv[1])