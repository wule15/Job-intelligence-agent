"""
Generate personalized cover letters using Claude API.
Matches jobs to best CV and creates custom cover letters.
"""

import json
from pathlib import Path
from anthropic import Anthropic
from core.config import Config
from core.database import Database
from core.utils import setup_logging, format_cv_label

logger = setup_logging('cover_letter_generator')

# ── Role-aware pitch style ────────────────────────────────────────────────────
_PITCH_STYLES = {
    'SalesEngineer_Industrial': (
        "Consultative technical sales: lead with engineering credibility and oil & gas domain knowledge. "
        "Emphasise pre-sales cycle ownership, EUR 300K+ proposal delivery, and ability to translate "
        "complex technical requirements into commercial outcomes for procurement and engineering stakeholders."
    ),
    'SalesEngineer_Tech': (
        "Solutions engineering / pre-sales for SaaS or tech products: lead with AI-assisted workflows, "
        "HubSpot/CRM proficiency, and ability to run POCs and technical demos. Emphasise funnel ownership, "
        "stakeholder presentations, and converting technical evaluations into closed deals."
    ),
    'AppointmentSetter': (
        "High-volume outbound SDR: lead with cold outreach volume, pipeline generated (EUR 150K), "
        "and AI-assisted prospecting tools. Emphasise discovery calls, lead qualification (BANT), "
        "CRM hygiene, and opening new markets from zero. Energetic, metrics-driven tone."
    ),
    'Content_Strategy': (
        "Content strategy and SEO: lead with buyer journey mapping, GA4/Semrush analysis, and "
        "conversion optimisation. Emphasise translating technical content into funnel assets and "
        "the ability to bridge engineering expertise with content marketing goals."
    ),
    'AI_Updated': (
        "AI-augmented engineering or operations: lead with AI tooling (Claude, Cursor, Zapier/Make), "
        "workflow automation, and version-controlled pipelines. Emphasise practical deployment of "
        "LLMs in real business contexts, not theoretical knowledge."
    ),
    'Engineering': (
        "Mechanical / process engineering: lead with hands-on engineering experience, project delivery, "
        "and technical depth in oil & gas / industrial systems. Emphasis on precision, problem-solving, "
        "and ability to own projects end-to-end."
    ),
    'Aerodynamics': (
        "CFD and aerodynamics engineering: lead with simulation expertise, analytical rigour, and "
        "academic depth in fluid dynamics. Emphasise research capability and technical specialisation."
    ),
}

def _pitch_style_for_cv(cv_hint: str) -> str:
    """Return the appropriate pitch style string for a given CV name hint."""
    if not cv_hint:
        return "Professional and concise — highlight the most relevant skills for this specific role."
    # Strip the configured filename prefix so the remainder matches a style key
    key = (format_cv_label(cv_hint) or '').replace(' ', '_').strip()
    return _PITCH_STYLES.get(key, (
        "Professional and concise — highlight the most relevant skills for this specific role."
    ))


class CoverLetterGenerator:
    """Generate personalized cover letters for job applications."""

    def __init__(self):
        self.client = Anthropic(api_key=Config.ANTHROPIC_API_KEY)
        self.db = Database()
        self.skills_data = self.load_skills()

    def load_skills(self):
        """Load extracted skills data."""
        try:
            skills_file = Path(Config.KEYWORDS_CACHE)
            with open(skills_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading skills: {e}")
            return {}

    def get_best_cv(self, job_description):
        """
        Select the best CV for this job based on skill match.
        Returns CV name and data.
        """
        best_cv = None
        best_score = 0
        desc_lower = job_description.lower()

        for cv_name, cv_data in self.skills_data.get('cvs', {}).items():
            if not isinstance(cv_data, dict) or 'skills' not in cv_data:
                continue

            # Count skill matches
            matches = sum(1 for skill in cv_data['skills'].keys()
                         if skill.lower() in desc_lower)

            if matches > best_score:
                best_score = matches
                best_cv = (cv_name, cv_data)

        return best_cv if best_cv else ('Primary', {})

    def generate_cover_letter(self, job_title, company, job_description,
                             selected_cv=None, cv_name_hint=None):
        """
        Generate a personalized cover letter.

        Args:
            job_title:     Job title
            company:       Company name
            job_description: Job description text
            selected_cv:   Optional CV data dict to use directly
            cv_name_hint:  CV filename stem from DB best_cv field (preferred)

        Returns:
            Cover letter text
        """
        # Resolve CV: prefer explicit hint from DB scorer, then auto-select, then fallback
        if cv_name_hint and not selected_cv:
            cv_data = self.skills_data.get('cvs', {}).get(cv_name_hint)
            if cv_data:
                selected_cv = cv_data
                cv_name = format_cv_label(cv_name_hint) or cv_name_hint
            else:
                cv_name, selected_cv = self.get_best_cv(job_description)
                cv_name = cv_name[0] if isinstance(cv_name, tuple) else cv_name
        elif not selected_cv:
            cv_name, selected_cv = self.get_best_cv(job_description)
            cv_name = cv_name[0] if isinstance(cv_name, tuple) else cv_name
        else:
            cv_name = "Primary"

        if isinstance(selected_cv, tuple):
            cv_name, selected_cv = selected_cv

        # Extract relevant info from CV
        cv_skills = list((selected_cv or {}).get('skills', {}).keys())[:8]
        cv_goals = (selected_cv or {}).get('goals', '')
        cv_experience = (selected_cv or {}).get('experience', [])
        cv_achievements = (selected_cv or {}).get('achievements', [])[:2]

        exp_summary = '; '.join(
            f"{e.get('role', '')} at {e.get('company', '')}"
            for e in cv_experience[:3] if e.get('role')
        )

        # Role-aware pitch style based on which CV was matched
        pitch_style = _pitch_style_for_cv(cv_name_hint or cv_name)

        prompt = f"""Write a concise, professional cover letter for: {job_title} at {company}

Candidate profile ({cv_name} CV):
- Key Skills: {', '.join(cv_skills)}
- Experience: {exp_summary}
- Goals: {cv_goals}
- Achievements: {'; '.join(cv_achievements)}

Job excerpt: {job_description[:600]}

Pitch style: {pitch_style}

Instructions: 3 short paragraphs. Tailor to the specific role and pitch style above. Start "Dear Hiring Manager,".
End with "Best regards,\\n{Config.CANDIDATE_NAME}". Do not add extra commentary."""

        try:
            message = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=600,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )

            cover_letter = message.content[0].text
            logger.info(f"[+] Generated cover letter for {job_title} @ {company}")
            return cover_letter

        except Exception as e:
            logger.error(f"Error generating cover letter: {e}")
            return None

    def format_cover_letter(self, job_title, company, cover_letter_text):
        """
        Format cover letter with header and date.

        Returns:
            Formatted cover letter string
        """
        from datetime import datetime

        date_str = datetime.now().strftime("%B %d, %Y")

        # Signature block is built from .env so no personal detail is committed.
        # Any field left blank is omitted rather than printed empty.
        signature = [
            line for line in (
                Config.CANDIDATE_NAME,
                Config.CANDIDATE_EMAIL,
                f"LinkedIn: {Config.CANDIDATE_LINKEDIN}" if Config.CANDIDATE_LINKEDIN else "",
            ) if line
        ]

        formatted = "\n".join([
            date_str,
            "",
            cover_letter_text,
            "",
            "Sincerely,",
            *signature,
        ])
        return formatted.strip()


if __name__ == '__main__':
    generator = CoverLetterGenerator()

    # Test
    test_job = {
        'title': 'Senior Python Developer',
        'company': 'TechCorp',
        'description': 'We are looking for an experienced Python developer with expertise in Django and PostgreSQL for a fully remote role.'
    }

    letter = generator.generate_cover_letter(
        test_job['title'],
        test_job['company'],
        test_job['description']
    )

    if letter:
        formatted = generator.format_cover_letter(
            test_job['title'],
            test_job['company'],
            letter
        )
        print(formatted)
