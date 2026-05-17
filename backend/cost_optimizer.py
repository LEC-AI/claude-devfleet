"""
Cost Optimizer — Analyze spending and suggest optimizations

Uses batch analysis to:
- Identify expensive mission types
- Suggest model downgrades
- Recommend parallelization opportunities
- Estimate cost savings
"""

import json
import logging
from typing import Dict, List

from db import get_db

log = logging.getLogger("devfleet.cost_optimizer")


async def analyze_costs_and_optimize(project_id: str) -> dict:
    """
    Comprehensive cost analysis with optimization suggestions.

    Returns:
    {
        "current_spending": {...},
        "cost_by_mission_type": {...},
        "cost_by_model": {...},
        "optimization_opportunities": [...],
        "estimated_savings_usd": N,
        "savings_percent": N
    }
    """
    db = await get_db()

    # Get all sessions for this project
    sessions = await db.execute_fetchall("""
        SELECT s.*, m.mission_type, m.model, m.title
        FROM agent_sessions s
        JOIN missions m ON s.mission_id = m.id
        WHERE m.project_id = ?
        ORDER BY s.started_at DESC
    """, (project_id,))
    sessions = [dict(row) for row in sessions]

    # Get mission details
    missions = await db.execute_fetchall(
        "SELECT * FROM missions WHERE project_id = ?",
        (project_id,)
    )
    missions = [dict(row) for row in missions]

    await db.close()

    # Calculate current spending
    total_spent = sum(float(s.get("total_cost_usd", 0) or 0) for s in sessions)
    total_tokens = sum(int(s.get("total_tokens", 0) or 0) for s in sessions)
    total_sessions = len(sessions)

    current_spending = {
        "total_usd": round(total_spent, 2),
        "avg_per_session": round(total_spent / max(total_sessions, 1), 4),
        "total_tokens": total_tokens,
        "avg_tokens_per_session": round(total_tokens / max(total_sessions, 1), 0),
        "total_sessions": total_sessions
    }

    # Break down by mission type
    cost_by_mission_type = {}
    for session in sessions:
        mtype = session.get("mission_type", "unknown")
        cost = float(session.get("total_cost_usd", 0) or 0)

        if mtype not in cost_by_mission_type:
            cost_by_mission_type[mtype] = {
                "count": 0,
                "total_cost": 0,
                "avg_cost": 0,
                "percent_of_budget": 0
            }

        cost_by_mission_type[mtype]["count"] += 1
        cost_by_mission_type[mtype]["total_cost"] += cost

    for mtype in cost_by_mission_type:
        stats = cost_by_mission_type[mtype]
        stats["avg_cost"] = round(stats["total_cost"] / max(stats["count"], 1), 4)
        stats["percent_of_budget"] = round(
            100 * stats["total_cost"] / max(total_spent, 0.01), 1
        )

    # Break down by model
    cost_by_model = {}
    for session in sessions:
        model = session.get("model", "unknown")
        cost = float(session.get("total_cost_usd", 0) or 0)

        if model not in cost_by_model:
            cost_by_model[model] = {
                "count": 0,
                "total_cost": 0,
                "avg_cost": 0,
                "percent_of_budget": 0
            }

        cost_by_model[model]["count"] += 1
        cost_by_model[model]["total_cost"] += cost

    for model in cost_by_model:
        stats = cost_by_model[model]
        stats["avg_cost"] = round(stats["total_cost"] / max(stats["count"], 1), 4)
        stats["percent_of_budget"] = round(
            100 * stats["total_cost"] / max(total_spent, 0.01), 1
        )

    # Identify optimization opportunities
    optimizations = _identify_optimizations(
        cost_by_mission_type,
        cost_by_model,
        sessions,
        total_spent
    )

    # Calculate potential savings
    estimated_savings = sum(opt.get("potential_savings", 0) for opt in optimizations)
    savings_percent = round(
        100 * estimated_savings / max(total_spent, 0.01), 1
    )

    return {
        "project_id": project_id,
        "current_spending": current_spending,
        "cost_by_mission_type": cost_by_mission_type,
        "cost_by_model": cost_by_model,
        "optimization_opportunities": optimizations,
        "estimated_savings_usd": round(estimated_savings, 2),
        "savings_percent": savings_percent,
        "recommendations": _generate_cost_recommendations(
            optimizations,
            estimated_savings,
            savings_percent
        )
    }


def _identify_optimizations(
    cost_by_type: Dict,
    cost_by_model: Dict,
    sessions: List[dict],
    total_spent: float
) -> List[dict]:
    """Identify specific cost optimization opportunities."""
    opportunities = []

    # 1. Detect expensive mission types that could use cheaper models
    for mtype, stats in cost_by_type.items():
        avg_cost = stats["avg_cost"]
        # If mission type averages > $3 and makes up significant budget
        if avg_cost > 3 and stats["percent_of_budget"] > 10:
            # Estimate savings if using sonnet instead of opus
            # Rough: Sonnet is ~60% of Opus cost
            potential_savings = stats["total_cost"] * 0.40
            opportunities.append({
                "type": "mission_model_downgrade",
                "mission_type": mtype,
                "current_avg_cost": avg_cost,
                "current_model": "likely claude-opus-4-7",
                "suggested_model": "claude-sonnet-4-6",
                "potential_savings": round(potential_savings, 2),
                "rationale": f"{mtype} missions average ${avg_cost:.2f}. These could use Sonnet.",
                "risk_level": "low"
            })

    # 2. Detect high-volume missions that accumulate costs
    for mtype, stats in cost_by_type.items():
        if stats["count"] > 5 and stats["avg_cost"] > 1:
            total_type_cost = stats["total_cost"]
            # Estimate 30% savings through parallelization
            potential_savings = total_type_cost * 0.15
            opportunities.append({
                "type": "parallelization",
                "mission_type": mtype,
                "session_count": stats["count"],
                "total_cost": round(stats["total_cost"], 2),
                "suggested_action": "Review mission dependencies to enable parallel execution",
                "potential_savings": round(potential_savings, 2),
                "rationale": f"Many {mtype} missions run sequentially. Consider parallel execution.",
                "risk_level": "medium"
            })

    # 3. Detect inefficient session patterns
    long_sessions = [s for s in sessions if int(s.get("total_tokens", 0) or 0) > 50000]
    if long_sessions:
        potential_savings = sum(
            float(s.get("total_cost_usd", 0) or 0) * 0.20
            for s in long_sessions
        )
        opportunities.append({
            "type": "session_optimization",
            "count": len(long_sessions),
            "avg_tokens_per_session": round(
                sum(int(s.get("total_tokens", 0) or 0) for s in long_sessions) / len(long_sessions),
                0
            ),
            "suggested_action": "Break large missions into smaller, focused subtasks",
            "potential_savings": round(potential_savings, 2),
            "rationale": f"{len(long_sessions)} sessions exceeded 50k tokens. Smaller tasks are more efficient.",
            "risk_level": "low"
        })

    # Sort by savings potential
    opportunities.sort(key=lambda x: x.get("potential_savings", 0), reverse=True)

    return opportunities


def _generate_cost_recommendations(
    opportunities: List[dict],
    total_savings: float,
    savings_percent: float
) -> List[str]:
    """Generate actionable cost recommendations."""
    recommendations = []

    if not opportunities:
        recommendations.append("✅ Your project spending appears efficient!")
        return recommendations

    # Summarize top opportunities
    top_opportunity = opportunities[0] if opportunities else None

    if total_savings > 50:
        recommendations.append(
            f"💰 You could save ${total_savings:.2f} ({savings_percent}%) by implementing "
            f"the recommendations below."
        )

    for opp in opportunities[:3]:  # Top 3
        opp_type = opp.get("type", "unknown")

        if opp_type == "mission_model_downgrade":
            recommendations.append(
                f"• {opp['mission_type']} missions: Switch from Opus to Sonnet "
                f"(save ${opp['potential_savings']:.2f})"
            )
        elif opp_type == "parallelization":
            recommendations.append(
                f"• Enable parallelization for {opp['mission_type']} "
                f"({opp['session_count']} sequential sessions, save ${opp['potential_savings']:.2f})"
            )
        elif opp_type == "session_optimization":
            recommendations.append(
                f"• Break large missions into smaller tasks "
                f"({opp['count']} long sessions, save ${opp['potential_savings']:.2f})"
            )

    # General advice
    if savings_percent > 20:
        recommendations.append(
            "💡 Consider implementing these optimizations incrementally "
            "and measure impact before full rollout."
        )

    return recommendations
