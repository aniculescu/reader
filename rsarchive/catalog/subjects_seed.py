#!/usr/bin/env python3
"""
rsarchive.catalog.subjects_seed — the hierarchical subject taxonomy seed.

This is *data*, loaded by ``catalog.subjects.seed_subjects`` into the ``subjects``
table. Keep it declarative and dependency-free so it round-trips cleanly and diffs
nicely in version control.

Shape
-----
``SUBJECTS`` is an ordered list of node dicts. Each node:

    {
        "slug": "gospel-study",          # stable unique id (used by CLI --tag and reseed)
        "name": "Gospel study",          # display name
        "description": "...",            # optional
        "children": [ <node>, ... ],     # optional, same shape, order preserved
    }

Order matters. ``seq`` in the ``subjects`` table is assigned from each node's index
among its siblings (0-based), so the *stated order of study* is preserved — this is
required for "Gospel study", whose child order (Matthew, Mark, Luke, John) is the
intended reading order.

``SEQUENCES`` seeds the ``sequences`` table with named ordered study orders. Items are
left empty at seed time (lectures are attached later, once imported); the scaffolding
exists so ordered study orders can be recorded immediately.

Everything here is additive: adding a node or a sequence and re-running
``catalog subjects --seed`` is idempotent (upsert by ``slug`` / ``name``).

Schema v3 adds two more seeds:

* ``PERSPECTIVES`` — the controlled vocabulary of *points of view* from which a
  lecture approaches a subject (loaded by ``subjects.seed_perspectives``, upsert by
  slug, ``seq`` = list order). Append to extend it.
* ``WRITTEN_WORKS`` / ``WORK_ALIASES`` — Steiner's major written works and the names
  they are cited by ("Occult Science", "Knowledge of the Higher Worlds", ...), mapped
  to their GA number (loaded by ``aliases.seed_work_aliases``; ``work_aliases`` rows
  need the GA to exist in ``volumes`` — see ``aliases.ensure_work_volumes``).
"""

from __future__ import annotations

from typing import Dict, Iterator, List, Optional, Tuple, TypedDict


class SubjectNode(TypedDict, total=False):
    slug: str
    name: str
    description: str
    children: List["SubjectNode"]


# --------------------------------------------------------------------------- taxonomy

SUBJECTS: List[SubjectNode] = [
    {
        "slug": "gospel-study",
        "name": "Gospel study",
        "description": (
            "Study of the four Gospels. Child order encodes the intended ORDER OF "
            "STUDY and is preserved via the subjects.seq column."
        ),
        "children": [
            {"slug": "gospel-matthew", "name": "Gospel of Matthew"},
            {"slug": "gospel-mark", "name": "Gospel of Mark"},
            {"slug": "gospel-luke", "name": "Gospel of Luke"},
            {"slug": "gospel-john", "name": "Gospel of John"},
        ],
    },
    {
        "slug": "christ-impulse",
        "name": "The Christ Impulse",
    },
    {
        "slug": "christ-event-present-future",
        "name": "The Christ Event and its meaning for our present and future",
    },
    {
        "slug": "karma",
        "name": "Karma",
    },
    {
        "slug": "soul-physiology",
        "name": "Soul Physiology",
    },
    {
        "slug": "music",
        "name": "Music",
    },
    {
        "slug": "arts-general",
        "name": "Arts (general)",
    },
    {
        "slug": "education",
        "name": "Education",
    },
    {
        "slug": "religion",
        "name": "Religion",
    },
    {
        "slug": "agriculture",
        "name": "Agriculture",
    },
    {
        "slug": "social-organization",
        "name": "Social Organization",
    },
    # ... room to add more: append nodes here (or nest under an existing node's
    # "children"); reseeding is idempotent by slug.
]


# ---------------------------------------------------------------------- sequences

class SequenceSeed(TypedDict, total=False):
    name: str
    description: str


# Named ordered study orders (sequence_items left empty at seed time).
SEQUENCES: List[SequenceSeed] = [
    {
        "name": "Gospel study order",
        "description": (
            "Recommended reading order across the Gospel lecture cycles. "
            "Populate sequence_items once the relevant lectures are imported."
        ),
    },
]


# -------------------------------------------------------------------- perspectives

class PerspectiveSeed(TypedDict, total=False):
    slug: str
    name: str
    description: str


# The point of view from which a lecture approaches a theme. Order = display order
# (perspectives.seq). Reseeding is idempotent by slug; append new ones at the end.
PERSPECTIVES: List[PerspectiveSeed] = [
    {
        "slug": "cosmological",
        "name": "Cosmological",
        "description": "The theme seen against the cosmos: sun, moon, planetary "
                       "spheres, stars, and the cosmic origin of the human being.",
    },
    {
        "slug": "evolutionary-historical",
        "name": "Evolutionary-historical",
        "description": "The theme traced through the evolution of earth and "
                       "humanity: cultural epochs, history, changing consciousness.",
    },
    {
        "slug": "physiological-medical",
        "name": "Physiological-medical",
        "description": "The theme as it lives in the human organism: the members of "
                       "the human being, bodily processes, illness and healing.",
    },
    {
        "slug": "christological",
        "name": "Christological",
        "description": "The theme in the light of the Christ Event, the Mystery of "
                       "Golgotha and the Gospels.",
    },
    {
        "slug": "artistic",
        "name": "Artistic",
        "description": "The theme approached through the arts: colour, music, "
                       "speech, eurythmy, architecture, the experience of beauty.",
    },
    {
        "slug": "social-practical",
        "name": "Social-practical",
        "description": "The theme applied to social and practical life: "
                       "threefolding, economics, community, everyday tasks.",
    },
    {
        "slug": "meditative-path",
        "name": "Meditative path",
        "description": "The theme as an inner path: exercises, meditation and the "
                       "stages of higher knowledge.",
    },
    {
        "slug": "pedagogical",
        "name": "Pedagogical",
        "description": "The theme from the standpoint of education and the "
                       "development of the child.",
    },
    {
        "slug": "natural-scientific",
        "name": "Natural-scientific",
        "description": "The theme related to natural science: Goethean observation, "
                       "physics, chemistry, biology, agriculture.",
    },
]


# ------------------------------------------------------------------- written works

class WrittenWork(TypedDict, total=False):
    ga: str                 # bare GA token (volumes.ga)
    title_en: str           # a standard English title (used for stub volumes)
    title_de: str           # German title (used for stub volumes)
    aliases: List[str]      # further names the work is cited by (EN translations, short DE)


# Steiner's major written works, cited in lectures as "my book <title>". Every GA was
# checked against the catalog before being listed here:
#   * in `volumes` with a matching title: GA 15, 25, 26;
#   * cited with these GA numbers in this repo's own editions (scripts/build_ga011_docx.py,
#     scripts/build_ga178_behind_scenes.py): GA 9, 11, 13;
#   * rsarchive serves "The Way of Initiation" under /Books/GA010/ (lectures.csv): GA 10;
#   * the remaining numbers are the standard Gesamtausgabe numbering (absent from
#     `volumes`, whose RSCatalog source lists lecture volumes only).
# Deliberately left out: GA 2 (the catalog's `volumes` row "2" is a mis-filed stub —
# "Faust, the Striving Human Being" — so it cannot be verified), GA 24 ("The Renewal
# of the Social Organism" is also GA 330's catalog title), "The Riddle of the Human
# Being" for GA 20 (also GA 170's catalog title), and bare "Initiation" (too generic:
# a seed alias is matched by every cue family, not just "my book ...").
# "Mystery Plays" / "Mystery Dramas" are safe for GA 14 because a seed title refuses a
# following "of" ("the mystery plays of the Middle Ages") and the cycle family needs
# lectures, which GA 14 has none of.
# Each title is normalised by aliases.normalize_alias, so case, a leading English
# article, quotes and punctuation do not matter (German articles DO: list the short
# form too, e.g. "Philosophie der Freiheit").
WRITTEN_WORKS: List[WrittenWork] = [
    {"ga": "3", "title_en": "Truth and Knowledge",
     "title_de": "Wahrheit und Wissenschaft",
     "aliases": ["Truth and Science"]},
    {"ga": "4", "title_en": "The Philosophy of Freedom",
     "title_de": "Die Philosophie der Freiheit",
     "aliases": ["The Philosophy of Spiritual Activity",
                 "Intuitive Thinking as a Spiritual Path",
                 "Philosophie der Freiheit"]},
    {"ga": "6", "title_en": "Goethe's World View",
     "title_de": "Goethes Weltanschauung",
     "aliases": ["Goethe's World-Conception", "Goethe's Conception of the World"]},
    {"ga": "7", "title_en": "Mysticism at the Dawn of the Modern Age",
     "title_de": "Die Mystik im Aufgange des neuzeitlichen Geisteslebens",
     "aliases": ["Mystics of the Renaissance", "Mystics after Modernism",
                 "Mystik im Aufgange des neuzeitlichen Geisteslebens"]},
    {"ga": "8", "title_en": "Christianity as Mystical Fact",
     "title_de": "Das Christentum als mystische Tatsache",
     "aliases": ["Christianity as a Mystical Fact",
                 "Christianity as Mystical Fact and the Mysteries of Antiquity",
                 "Christentum als mystische Tatsache"]},
    {"ga": "9", "title_en": "Theosophy",
     "title_de": "Theosophie",
     "aliases": []},
    {"ga": "10", "title_en": "Knowledge of the Higher Worlds and Its Attainment",
     "title_de": "Wie erlangt man Erkenntnisse der höheren Welten?",
     "aliases": ["Knowledge of the Higher Worlds",
                 "Knowledge of Higher Worlds",
                 "Knowledge of Higher Worlds and Its Attainment",
                 "How to Know Higher Worlds",
                 "How Does One Attain Knowledge of the Higher Worlds",
                 "How to Attain Knowledge of the Higher Worlds",
                 "Initiation and Its Results",
                 "The Way of Initiation"]},
    {"ga": "11", "title_en": "Cosmic Memory",
     "title_de": "Aus der Akasha-Chronik",
     "aliases": ["From the Akashic Record", "From the Akasha Chronicle"]},
    {"ga": "12", "title_en": "The Stages of Higher Knowledge",
     "title_de": "Die Stufen der höheren Erkenntnis",
     "aliases": ["Stufen der höheren Erkenntnis"]},
    {"ga": "13", "title_en": "An Outline of Occult Science",
     "title_de": "Die Geheimwissenschaft im Umriss",
     "aliases": ["Occult Science", "Occult Science: An Outline",
                 "An Outline of Esoteric Science",
                 "Geheimwissenschaft im Umriss", "Geheimwissenschaft"]},
    {"ga": "14", "title_en": "Four Mystery Dramas",
     "title_de": "Vier Mysteriendramen",
     "aliases": ["Four Mystery Plays", "The Portal of Initiation",
                 "Mystery Plays", "Mystery Dramas"]},
    {"ga": "15", "title_en": "The Spiritual Guidance of Mankind",
     "title_de": "Die geistige Führung des Menschen und der Menschheit",
     "aliases": ["The Spiritual Guidance of Man",
                 "The Spiritual Guidance of Man and Humanity",
                 "The Spiritual Guidance of the Individual and Humanity",
                 "Geistige Führung des Menschen und der Menschheit"]},
    {"ga": "16", "title_en": "A Road to Self-Knowledge",
     "title_de": "Ein Weg zur Selbsterkenntnis des Menschen",
     "aliases": ["A Way of Self-Knowledge"]},
    {"ga": "17", "title_en": "The Threshold of the Spiritual World",
     "title_de": "Die Schwelle der geistigen Welt",
     "aliases": []},
    {"ga": "18", "title_en": "The Riddles of Philosophy",
     "title_de": "Die Rätsel der Philosophie",
     "aliases": []},
    {"ga": "20", "title_en": "The Riddle of Man",
     "title_de": "Vom Menschenrätsel",
     "aliases": []},
    {"ga": "21", "title_en": "Riddles of the Soul",
     "title_de": "Von Seelenrätseln",
     "aliases": ["The Case for Anthroposophy"]},
    {"ga": "23", "title_en": "Towards Social Renewal",
     "title_de": "Die Kernpunkte der sozialen Frage",
     "aliases": ["The Threefold Commonwealth", "The Threefold Social Order",
                 "Basic Issues of the Social Question",
                 "Kernpunkte der sozialen Frage"]},
    {"ga": "25", "title_en": "Cosmology, Religion and Philosophy",
     "title_de": "Kosmologie, Religion und Philosophie",
     "aliases": []},
    {"ga": "26", "title_en": "Anthroposophical Leading Thoughts",
     "title_de": "Anthroposophische Leitsätze",
     "aliases": []},
    {"ga": "27", "title_en": "Fundamentals of Therapy",
     "title_de": "Grundlegendes für eine Erweiterung der Heilkunst",
     "aliases": ["Extending Practical Medicine"]},
    {"ga": "28", "title_en": "The Course of My Life",
     "title_de": "Mein Lebensgang",
     "aliases": ["Autobiography", "The Story of My Life"]},
]


def _work_aliases(works: List[WrittenWork]) -> List[Tuple[str, str]]:
    """Flatten ``WRITTEN_WORKS`` into ``(alias, ga)`` pairs: each work's English
    title, German title, then its listed aliases, in declaration order."""
    out: List[Tuple[str, str]] = []
    for w in works:
        for name in [w.get("title_en", ""), w.get("title_de", "")] + list(
                w.get("aliases") or []):
            if name:
                out.append((name, w["ga"]))
    return out


# English titles of LECTURE volumes that translations cite but the catalog's own
# titles_in_volume line lacks (so no derived alias exists for them).
LECTURE_VOLUME_ALIASES: List[Tuple[str, str]] = [
    ("Origins of Modern Science", "326"),     # GA 326, "The Origins of Natural Science"
]

# ``(alias, ga)`` pairs seeded into work_aliases with source='seed' (they always win
# over aliases derived automatically from volume / cycle titles).
WORK_ALIASES: List[Tuple[str, str]] = (
    _work_aliases(WRITTEN_WORKS) + LECTURE_VOLUME_ALIASES)


# ------------------------------------------------------------------------- helpers

def iter_subjects(
    nodes: Optional[List[SubjectNode]] = None,
    parent_slug: Optional[str] = None,
) -> Iterator[Tuple[SubjectNode, Optional[str], int]]:
    """Depth-first walk yielding ``(node, parent_slug, seq)`` in declaration order.

    ``seq`` is the 0-based index of the node among its siblings (its order of study).
    Consumers (``seed_subjects``) resolve ``parent_slug`` to a ``subjects.id`` and
    insert in this order so parents are created before their children.
    """
    for seq, node in enumerate(nodes if nodes is not None else SUBJECTS):
        yield node, parent_slug, seq
        children = node.get("children") or []
        if children:
            yield from iter_subjects(children, node.get("slug"))


def flatten() -> List[Dict[str, object]]:
    """Convenience: a flat list of ``{slug, name, description, parent_slug, seq}`` dicts
    in insertion order. Useful for tests and for a non-recursive seeder."""
    out: List[Dict[str, object]] = []
    for node, parent_slug, seq in iter_subjects():
        out.append({
            "slug": node.get("slug"),
            "name": node["name"],
            "description": node.get("description", ""),
            "parent_slug": parent_slug,
            "seq": seq,
        })
    return out


if __name__ == "__main__":
    for row in flatten():
        indent = "  " if row["parent_slug"] else ""
        print(f"{indent}{row['seq']}. {row['name']}  [{row['slug']}]")
    print(f"\n{len(flatten())} subjects, {len(SEQUENCES)} sequence(s).")
