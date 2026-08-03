"""
Keyword extraction from CVs and LinkedIn profile using Claude API.
Extracts skills, experience, and other relevant information for job matching.
"""

import json
from pathlib import Path
from anthropic import Anthropic
from core.config import Config
from core.utils import setup_logging
import pdfplumber

logger = setup_logging('keyword_extractor')

class KeywordExtractor:
    """Extract keywords and skills from CVs and LinkedIn profile."""

    def __init__(self):
        self.client = Anthropic(api_key=Config.ANTHROPIC_API_KEY)
        self.extracted_data = {
            'cvs': {},
            'linkedin': None,
            'merged_skills': []
        }

    def extract_cv_text(self, cv_path):
        """Extract text from PDF CV file."""
        try:
            with pdfplumber.open(cv_path) as pdf:
                text = ""
                for page in pdf.pages:
                    text += page.extract_text() + "\n"
            return text
        except Exception as e:
            logger.error(f"Error extracting text from {cv_path}: {e}")
            return None

    def extract_linkedin_text(self):
        """Extract text from LinkedIn profile file."""
        linkedin_dir = Config.LINKEDIN_DIR
        linkedin_files = list(linkedin_dir.glob('*'))

        if not linkedin_files:
            logger.warning("No LinkedIn profile file found")
            return None

        linkedin_file = linkedin_files[0]
        logger.info(f"Reading LinkedIn profile from: {linkedin_file}")

        try:
            if linkedin_file.suffix.lower() == '.pdf':
                return self.extract_cv_text(linkedin_file)
            else:
                # Assume text file
                with open(linkedin_file, 'r', encoding='utf-8') as f:
                    return f.read()
        except Exception as e:
            logger.error(f"Error reading LinkedIn file {linkedin_file}: {e}")
            return None

    def extract_skills_from_text(self, text, source_name):
        """Use Claude API to extract skills from CV/LinkedIn text."""
        # Use full CV text (up to 12000 chars) so skills buried in experience sections are captured
        safe_text = text[:12000].replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')

        prompt = f"""Extract ALL skills and tools from this CV. Include both hard skills (software, tools, technologies, methodologies) and transferable skills (sales, project management, etc). Use the exact terms a job posting would use — prefer common/generic names over proprietary ones where applicable (e.g. "CFD" not just "Star-CCM+"). Return ONLY valid JSON (no other text).

{{
  "skills": {{"Skill Name": years_of_experience}},
  "experience": [{{"role": "Title", "company": "Name", "years": "X"}}],
  "goals": "Career goal",
  "achievements": ["major achievement"],
  "key_domains": ["domain1", "domain2"]
}}

CV: {safe_text}"""

        try:
            message = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1500,
                messages=[
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]
            )

            # Parse response
            response_text = message.content[0].text

            # Try to extract JSON from response
            try:
                json_start = response_text.find('{')
                json_end = response_text.rfind('}') + 1
                if json_start >= 0 and json_end > json_start:
                    json_str = response_text[json_start:json_end]
                    try:
                        skills_data = json.loads(json_str)
                        logger.info(f"[+] Extracted skills from {source_name}")
                        return skills_data
                    except json.JSONDecodeError as je:
                        logger.warning(f"Failed to parse JSON: {je}")
                        # Show problematic section
                        error_pos = je.pos
                        start = max(0, error_pos - 50)
                        end = min(len(json_str), error_pos + 50)
                        logger.warning(f"Problem area: ...{json_str[start:end]}...")
                        return None
                else:
                    logger.warning(f"Could not find JSON in Claude response for {source_name}")
                    return None
            except Exception as e:
                logger.warning(f"Error extracting JSON: {e}")
                logger.debug(f"Response was: {response_text}")
                return None

        except Exception as e:
            logger.error(f"Error calling Claude API: {e}")
            return None

    def extract_all_cvs(self):
        """Extract skills from all CV files in resumes directory."""
        cv_dir = Config.RESUMES_DIR
        cv_files = list(cv_dir.glob('*.pdf'))

        if not cv_files:
            logger.warning(f"No CV files found in {cv_dir}")
            return False

        logger.info(f"Found {len(cv_files)} CV file(s) to process")

        for cv_path in cv_files:
            cv_name = cv_path.stem
            logger.info(f"Processing: {cv_path.name}")

            # Extract text
            text = self.extract_cv_text(cv_path)
            if not text:
                logger.warning(f"Skipping {cv_name} - could not extract text")
                continue

            # Extract skills using Claude
            skills_data = self.extract_skills_from_text(text, cv_name)
            if skills_data:
                self.extracted_data['cvs'][cv_name] = skills_data
            else:
                logger.warning(f"Failed to extract skills from {cv_name}")

        return len(self.extracted_data['cvs']) > 0

    def extract_linkedin(self):
        """Extract skills from LinkedIn profile."""
        logger.info("Processing LinkedIn profile")

        text = self.extract_linkedin_text()
        if not text:
            logger.warning("Could not read LinkedIn profile")
            return False

        skills_data = self.extract_skills_from_text(text, "LinkedIn Profile")
        if skills_data:
            self.extracted_data['linkedin'] = skills_data
            logger.info("[+] Extracted LinkedIn profile data")
            return True
        else:
            logger.warning("Failed to extract skills from LinkedIn profile")
            return False

    def merge_skills(self):
        """Merge skills from all CVs and LinkedIn profile."""
        merged_skills = {}

        # Collect skills from all CVs
        for cv_name, cv_data in self.extracted_data['cvs'].items():
            if 'skills' in cv_data:
                for skill, years in cv_data['skills'].items():
                    if skill not in merged_skills:
                        merged_skills[skill] = []
                    merged_skills[skill].append({
                        'source': cv_name,
                        'years': years
                    })

        # Add LinkedIn skills
        if self.extracted_data['linkedin'] and 'skills' in self.extracted_data['linkedin']:
            for skill in self.extracted_data['linkedin']['skills'].keys():
                if skill not in merged_skills:
                    merged_skills[skill] = []
                merged_skills[skill].append({
                    'source': 'LinkedIn',
                    'years': 'Endorsed'
                })

        self.extracted_data['merged_skills'] = list(merged_skills.keys())
        logger.info(f"[+] Merged {len(merged_skills)} unique skills from all sources")

        return merged_skills

    def save_to_cache(self):
        """Save extracted data to JSON cache."""
        cache_path = Path(Config.KEYWORDS_CACHE)

        try:
            with open(cache_path, 'w') as f:
                json.dump(self.extracted_data, f, indent=2)
            logger.info(f"[+] Saved extracted data to {cache_path}")
            return True
        except Exception as e:
            logger.error(f"Error saving cache: {e}")
            return False

    def run(self):
        """Run full keyword extraction process."""
        logger.info("Starting keyword extraction process...")

        # Extract from CVs
        if not self.extract_all_cvs():
            logger.error("Failed to extract skills from any CVs")
            return False

        # Extract from LinkedIn (optional)
        self.extract_linkedin()

        # Merge all skills
        self.merge_skills()

        # Save to cache
        if not self.save_to_cache():
            logger.error("Failed to save extracted data")
            return False

        logger.info("[+] Keyword extraction complete!")
        return True


def main():
    """Run keyword extraction."""
    extractor = KeywordExtractor()
    success = extractor.run()

    if success:
        logger.info("Extracted data summary:")
        logger.info(f"  - CVs processed: {len(extractor.extracted_data['cvs'])}")
        logger.info(f"  - LinkedIn profile: {'Yes' if extractor.extracted_data['linkedin'] else 'No'}")
        logger.info(f"  - Total unique skills: {len(extractor.extracted_data['merged_skills'])}")
        print("\n[+] Keywords extracted successfully!")
        print(f"  - CVs processed: {len(extractor.extracted_data['cvs'])}")
        print(f"  - Total skills extracted: {len(extractor.extracted_data['merged_skills'])}")
        return 0
    else:
        logger.error("Keyword extraction failed")
        print("\n[-] Failed to extract keywords")
        return 1


if __name__ == '__main__':
    import sys
    sys.exit(main())
