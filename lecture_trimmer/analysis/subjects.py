"""Subject and class-type briefs added to the editor instructions.

Detection is deterministic keyword scoring over the user's subject hint and the
transcript, so the same lecture always receives the same brief. A pack only adds
contrast examples; the core editorial brief is identical for every subject.
"""
import re

PACKS = {
    "physics": (
        "Physics. Questions are numericals and MCQs: a teacher states givens ('a ball is thrown at 20 m/s'),"
        " asks to find something, often runs a chat poll, then solves and reveals the answer. Deriving a"
        " general relation with symbols (v = u + at, t = u/g) is concept, even with a ball or car as the"
        " setting. Silent board work inside a solution belongs to that task. 'Short trick' segments and"
        " sign-convention explanations are academic.",
        ("velocity", "acceleration", "force", "momentum", "gravity", "projectile", "displacement",
         "newton", "energy", "current", "voltage", "resistance", "electric", "magnetic", "wave",
         "optics", "lens", "friction", "torque", "kinematics", "physics", "joule", "m/s")),
    "chemistry": (
        "Chemistry. Mnemonics, rhymes and songs that encode facts (periodic table orders, reactivity series)"
        " are academic, not off-topic. Reaction mechanisms, named reactions, colours, smells and exceptions are"
        " exam facts. Questions are 'predict the product', 'which is more acidic', MCQs and assertion-reason."
        " Explaining a mechanism with a sample molecule is illustration unless students are asked to answer.",
        ("reaction", "mole", "bond", "electron", "orbital", "acid", "base", "salt", "oxidation",
         "reduction", "organic", "alkane", "alkene", "benzene", "periodic", "valency", "molarity",
         "compound", "element", "chemistry", "hybridisation", "hybridization", "isomer")),
    "mathematics": (
        "Mathematics. Long silences usually mean board work and belong to the surrounding activity."
        " A second method or a shortcut for the same problem is academic. Proving a general theorem is"
        " concept; 'prove that ... for the given triangle' or 'find x' posed to students is a task."
        " Multiple solved examples in a row are separate tasks when each has its own question.",
        ("equation", "integral", "derivative", "differentiation", "integration", "matrix", "triangle",
         "theorem", "polynomial", "quadratic", "probability", "trigonometry", "sin", "cos", "tan",
         "limit", "function", "graph", "algebra", "geometry", "maths", "mathematics", "vector")),
    "biology": (
        "Biology. Reading NCERT lines and explaining them is core concept, even when slow or repetitive."
        " Diagram drawing with labelling is academic. Real-life cases used to explain (a coma patient,"
        " a disease) are illustrations. Questions are mostly MCQs, PYQs, assertion-reason and"
        " match-the-column; theory usually dominates a biology class. Motivation that mentions 'life' is"
        " still off-topic.",
        ("cell", "organism", "tissue", "species", "photosynthesis", "respiration", "enzyme", "dna",
         "gene", "chromosome", "reproduction", "plant", "animal", "kingdom", "ncert", "biology",
         "metabolism", "hormone", "organ", "protein", "taxonomy", "evolution", "ecosystem")),
    "social_science": (
        "History, civics, geography or economics. Narratives and stories ARE the content: a story that"
        " explains a movement, a reform or a cause is concept, not off-topic. Dates, names, maps and"
        " causes are exam facts. Current-affairs chat with no syllabus link is off-topic. Questions are"
        " source-based questions, answer-writing practice and MCQs.",
        ("history", "colonial", "movement", "revolution", "empire", "constitution", "parliament",
         "democracy", "geography", "economics", "civics", "british", "mughal", "independence",
         "climate", "resources", "census", "gdp")),
    "english": (
        "English or literature. Reciting or reading a poem, song or passage that is being analysed is"
        " academic. Grammar rules with example sentences are concept with illustrations. Questions are"
        " comprehension, grammar exercises and answer-writing. Life advice inspired by the text is"
        " off-topic unless it explains the text's theme.",
        ("poem", "poet", "chapter", "grammar", "tense", "noun", "verb", "adjective", "passage",
         "comprehension", "literature", "stanza", "metaphor", "author", "essay")),
    "computer_science": (
        "Computer science. Silent typing and running code are academic demonstrations. Debugging a"
        " compile or runtime error is teaching, not a technical issue; only real software, screen-share"
        " or connection trouble is technical. Questions are coding problems and output-prediction MCQs.",
        ("code", "program", "function", "loop", "array", "variable", "python", "java", "compile",
         "algorithm", "output", "syntax", "string", "class", "object", "recursion", "database")),
}

CLASS_TYPES = {
    "revision": ("One-shot or revision class: very dense; tighten very little and keep every fact.",
                 ("one shot", "oneshot", "revision", "marathon", "crash course")),
    "doubt": ("Doubt class: almost every student doubt with its answer is study (task_doubt or concept_doubt).",
              ("doubt class", "doubt session", "doubt clearing")),
    "test_discussion": ("Test or paper discussion: each discussed question is a task; score talk and ranks are logistics.",
                        ("test discussion", "paper discussion", "pyq", "previous year", "solution discussion")),
}


def _score(text, words):
    return sum(len(re.findall(r"\b" + re.escape(word) + r"\b", text)) for word in words)


def detect_subject(context, units):
    """Return (subject key or None, class-type key or None)."""
    hint = (context or "").lower()
    for key in PACKS:
        if key.replace("_", " ") in hint or (key == "mathematics" and re.search(r"\bmaths?\b", hint)):
            return key, _class_type(hint)
    for key, (_, words) in PACKS.items():
        if _score(hint, words):
            return key, _class_type(hint)
    sample = " ".join(u.text for u in units[:400]).lower()
    scores = {key: _score(sample, words) for key, (_, words) in PACKS.items()}
    best = max(scores, key=scores.get)
    ranked = sorted(scores.values(), reverse=True)
    # Ambiguous or keyword-poor transcripts get only the universal brief.
    if ranked[0] < 8 or ranked[0] < 1.5 * ranked[1]:
        return None, _class_type(hint)
    return best, _class_type(hint)


def _class_type(hint):
    for key, (_, words) in CLASS_TYPES.items():
        if any(word in hint for word in words):
            return key
    return None


def subject_brief(context, units):
    subject, class_type = detect_subject(context, units)
    lines = []
    if subject:
        lines.append("Subject notes: " + PACKS[subject][0])
    if class_type:
        lines.append("Class type: " + CLASS_TYPES[class_type][0])
    return subject, class_type, "\n".join(lines)
