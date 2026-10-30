"""Quality and bounded practice for the boundaries-course AI trainer.

No student replies are persisted by this module. Rubric and deterministic drills
make training feedback auditable rather than dependent on model compliments.
"""
from __future__ import annotations

import re

METRICS = ('clarity', 'respect', 'boundaries')
HYPE = re.compile(r'\b(?:идеал(?:ен|ьна|ьно|ьный)|безупречн\w*|совершенн(?:ый|о)|блестящ(?:ий|е)|эталонн\w*)\b', re.I)


def neutralize_praise(text: str) -> str:
    """Remove inflated verdicts without changing the student's quoted answers."""
    if not isinstance(text, str):
        return ''
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    return ' '.join(sentence for sentence in sentences if not HYPE.search(sentence)).strip()


def turn_metrics(turn: dict) -> dict[str, int]:
    scores = turn.get('signals') if isinstance(turn.get('signals'), dict) else {}
    return {k: scores.get(k) if type(scores.get(k)) is int and 0 <= scores[k] <= 2 else 1 for k in METRICS}


def practice_for_weakness(scenario: dict, turns: list[dict]) -> str:
    """Generate an actionable rehearsal using a specific low-scoring utterance."""
    if len(turns) != 3:
        raise ValueError('Three rounds are required')
    score = {metric: sum(turn_metrics(t)[metric] for t in turns) for metric in METRICS}
    # In a tie, favor the skill with the weakest single round, not an arbitrary label.
    # This catches a sharp outburst even when later turns were calm.
    weakest = min(('boundaries', 'respect', 'clarity'),
                  key=lambda k: (score[k], min(turn_metrics(t)[k] for t in turns)))
    if all(v >= 5 for v in score.values()):
        return (f'Перенос навыка: представь похожую просьбу по теме «{scenario["title"]}». '
                'Напиши две короткие версии отказа: без альтернативы и с альтернативой, которую действительно готов предложить. '
                'Затем произнеси обе вслух и проверь, что решение не изменилось.')
    i = min(range(3), key=lambda idx: turn_metrics(turns[idx])[weakest])
    student_text = str(turns[i].get('user', '')).replace('\n', ' ').strip()[:105]
    quote = f'«{student_text}»'
    if weakest == 'respect':
        return (f'Тренировка спокойного тона. Возьми фразу из раунда {i+1}: {quote}. '
                'Перепиши её в двух версиях: короткий отказ без оценки собеседника и такой же отказ с признанием его просьбы. '
                'Произнеси обе и отметь, какая сохраняет твёрдость без нападения.')
    if weakest == 'clarity':
        return (f'Тренировка ясности. Вернись к ответу из раунда {i+1}: {quote}. '
                'Сформулируй то же решение в одном предложении до 15 слов, начиная с конкретного действия «не смогу / не буду / не готов». '
                'Убери неопределённые обещания и прочитай вслух.')
    return (f'Тренировка удержания границы. Перепиши ответ из раунда {i+1}: {quote}, '
            'сохранив своё решение и не обещая уступку. Затем представь повторное давление собеседника '
            'и повтори отказ одной короткой фразой. Проверь, не изменилось ли решение.')


def finalize(result: dict, scenario: dict, round_no: int, message: str, history: list[dict]) -> dict:
    """Normalize hype and choose a measurable, turn-specific consolidation exercise."""
    result = dict(result)
    result['feedback'] = neutralize_praise(result.get('feedback', '')) or result.get('feedback', '')
    if round_no != 3:
        return result
    turns = [*history, {'user': message, 'signals': result.get('signals', {})}]
    result['next_practice'] = practice_for_weakness(scenario, turns)
    summary = neutralize_praise(result.get('final_summary', ''))
    if not summary:
        summary = 'Оцени не только последний ответ, но и изменения между тремя раундами. Конкретные подсказки есть в разборе каждого ответа.'
    if all(isinstance(t.get('signals'), dict) for t in history):
        trajectory = ' → '.join(f'{sum(turn_metrics(t).values())}/6' for t in turns)
        summary += f' Динамика оценок по раундам: {trajectory}. Эти баллы отражают только учебные реплики.'
    result['final_summary'] = summary[:1500]
    return result
