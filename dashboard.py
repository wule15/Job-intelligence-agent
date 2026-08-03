"""
Simple web dashboard to view found jobs.
Run: python dashboard.py
Then open: http://localhost:5000
"""

from flask import Flask, render_template, request, jsonify
from core.config import Config
from core.utils import format_cv_label
import sqlite3
from datetime import datetime

app = Flask(__name__)

# Industry detection keywords
INDUSTRIES = {
    'Engineering': ['engineer', 'mechanical', 'systems', 'process', 'cfd', 'solidworks', 'autocad', 'technical engineer', 'aerospace', 'structural'],
    'AI & Automation': ['ai', 'artificial intelligence', 'machine learning', 'automation', 'nlp', 'deep learning', 'llm', 'ml engineer', 'ai integration'],
    'Content & Strategy': ['content', 'strategist', 'copywriter', 'technical writer', 'documentation', 'seo', 'content marketing', 'blog', 'editorial'],
    'Sales Engineering': ['sales engineer', 'pre-sales', 'solutions engineer', 'technical sales', 'account engineer', 'value engineer'],
    'Oil & Gas': ['oil', 'gas', 'petroleum', 'upstream', 'downstream', 'refinery', 'pipeline', 'energy', 'oilfield'],
    'Project Management': ['project manager', 'program manager', 'scrum', 'agile', 'delivery manager', 'pmo'],
    'Architecture': ['solutions architect', 'cloud architect', 'enterprise architect', 'system architect'],
    'Software Development': ['software developer', 'backend', 'frontend', 'fullstack', 'python developer', 'javascript', 'devops'],
    'Data & Analytics': ['data engineer', 'data scientist', 'analytics', 'business intelligence', 'sql', 'data analyst'],
}

def extract_source_from_url(url, fallback='JSearch'):
    """Extract readable job site name from URL."""
    if not url:
        return fallback
    url_lower = url.lower()
    if 'linkedin.com' in url_lower:
        return 'LinkedIn'
    if 'indeed.com' in url_lower:
        return 'Indeed'
    if 'glassdoor.com' in url_lower:
        return 'Glassdoor'
    if 'ziprecruiter.com' in url_lower:
        return 'ZipRecruiter'
    if 'remoteok' in url_lower:
        return 'RemoteOK'
    if 'weworkremotely.com' in url_lower:
        return 'We Work Remotely'
    if 'flexjobs.com' in url_lower:
        return 'FlexJobs'
    if 'monster.com' in url_lower:
        return 'Monster'
    if 'careerbuilder.com' in url_lower:
        return 'CareerBuilder'
    if 'simplyhired.com' in url_lower:
        return 'SimplyHired'
    if 'dice.com' in url_lower:
        return 'Dice'
    if 'greenhouse.io' in url_lower:
        return 'Greenhouse'
    if 'lever.co' in url_lower:
        return 'Lever'
    if 'workday.com' in url_lower:
        return 'Workday'
    if 'smartrecruiters.com' in url_lower:
        return 'SmartRecruiters'
    if 'jobvite.com' in url_lower:
        return 'Jobvite'
    if 'icims.com' in url_lower:
        return 'iCIMS'
    if 'taleo.net' in url_lower:
        return 'Taleo'
    if 'hired.com' in url_lower:
        return 'Hired'
    if 'angel.co' in url_lower or 'wellfound.com' in url_lower:
        return 'Wellfound'
    if 'builtin.com' in url_lower:
        return 'Built In'
    if 'themuse.com' in url_lower:
        return 'The Muse'
    if 'stackoverflow.com' in url_lower or 'jobs.stackoverflow' in url_lower:
        return 'Stack Overflow'
    if 'ycombinator.com' in url_lower:
        return 'Y Combinator'
    # Extract domain as last resort
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc.lower()
        domain = domain.replace('www.', '').replace('jobs.', '')
        domain = domain.split('.')[0].capitalize()
        return domain if domain else fallback
    except Exception:
        return fallback


def detect_industry(title, description):
    """Detect job industry from title and description."""
    text = (title + ' ' + (description or '')).lower()
    for industry, keywords in INDUSTRIES.items():
        if any(kw in text for kw in keywords):
            return industry
    return 'Other'

def get_db():
    """Get direct database connection."""
    return sqlite3.connect(Config.DATABASE_PATH)

def run_migrations():
    """Ensure all schema migrations are applied."""
    try:
        conn = get_db()
        for col, definition in [('best_cv', 'TEXT'), ('seen_count', 'INTEGER DEFAULT 1'),
                                ('source_message_id', 'TEXT')]:
            try:
                conn.execute(f'ALTER TABLE jobs ADD COLUMN {col} {definition}')
            except Exception:
                pass
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Migration warning: {e}")

# Run migrations once at import time
run_migrations()

def init_application_status_table():
    """Create application_status table if it doesn't exist."""
    try:
        conn = get_db()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS application_status (
                job_id INTEGER PRIMARY KEY,
                status TEXT DEFAULT 'none',
                updated_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(job_id) REFERENCES jobs(id)
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error initializing application_status table: {e}")

@app.route('/')
def dashboard():
    """Main dashboard page."""
    init_application_status_table()
    min_score = request.args.get('min_score', 0, type=float)
    source = request.args.get('source', '', type=str)
    date_filter = request.args.get('date', '', type=str)
    industry_filter = request.args.get('industry', '', type=str)
    origin_filter = request.args.get('origin', '', type=str)  # 'email' or 'search'
    inbox_filter  = request.args.get('inbox', '', type=str)   # email account username
    sort_by = request.args.get('sort', 'score', type=str)  # score, date, salary

    try:
        conn = get_db()
        cursor = conn.cursor()

        # Build query dynamically
        conditions = ["relevance_score >= ?"]
        params = [min_score]

        if date_filter:
            conditions.append("DATE(extracted_date) = ?")
            params.append(date_filter)

        where_clause = " AND ".join(conditions)

        cursor.execute(f"""
            SELECT id, job_title, company, description, link, salary, source,
                   extracted_date, relevance_score,
                   COALESCE(seen_count, 1) as seen_count,
                   source_message_id
            FROM jobs WHERE {where_clause}
            ORDER BY extracted_date DESC, relevance_score DESC
        """, params)

        rows = cursor.fetchall()

        job_list = []
        for row in rows:
            desc = row[3] or ''
            raw_date = str(row[7]) if row[7] else ''
            formatted_date = raw_date[:10] if raw_date else ''
            industry = detect_industry(row[1] or '', desc)
            seen_count = row[9] if len(row) > 9 else 1
            raw_source = row[6] or ''
            origin = 'email' if raw_source.startswith('Email') else 'search'
            source_message_id = row[10] if len(row) > 10 else None
            email_account = ''
            if origin == 'email' and '/' in raw_source:
                full_addr = raw_source.split('/', 1)[1].strip()
                email_account = full_addr.split('@')[0]

            # Build Gmail search link
            # authuser=<email> forces the correct account regardless of u/0 vs u/1 slot.
            # Fragments must NOT have the colon encoded — Gmail receives them raw.
            gmail_url = None
            if origin == 'email':
                # Extract account email from source e.g. "Email / you@gmail.com"
                acct = ''
                if '/' in raw_source:
                    acct = raw_source.split('/', 1)[1].strip()
                auth = f'?authuser={acct}' if acct else ''

                if source_message_id:
                    mid = source_message_id.strip('<> ').replace(' ', '%20')
                    gmail_url = f'https://mail.google.com/mail/{auth}#search/rfc822msgid:{mid}'
                else:
                    company_q = (row[2] or '').replace(' ', '+')
                    title_words = '+'.join((row[1] or '').split()[:3])
                    gmail_url = f'https://mail.google.com/mail/{auth}#search/{company_q}+{title_words}'

            job_list.append({
                'id': row[0],
                'title': row[1],
                'company': row[2],
                'description': desc[:200] + '...' if len(desc) > 200 else desc,
                'link': row[4],
                'salary': row[5],
                'source': extract_source_from_url(row[4], fallback=raw_source or 'JSearch'),
                'date': formatted_date,
                'score': round(row[8], 1) if row[8] else 0,
                'industry': industry,
                'seen_count': seen_count,
                'relisted': seen_count > 1,
                'origin': origin,
                'gmail_url': gmail_url,
                'email_account': email_account,
            })

        # Get cover letter status for all jobs
        cursor.execute("SELECT job_id FROM cover_letters_sent")
        covered_job_ids = set(row[0] for row in cursor.fetchall())

        # Get application status for all jobs
        cursor.execute("SELECT job_id, status FROM application_status")
        status_map = {row[0]: row[1] for row in cursor.fetchall()}

        for job in job_list:
            job['has_cover_letter'] = job['id'] in covered_job_ids
            job['apply_status'] = status_map.get(job['id'], 'none')

            # Calculate days old
            if job['date']:
                from datetime import datetime
                posting_date = datetime.strptime(job['date'], '%Y-%m-%d')
                days_old = (datetime.now() - posting_date).days
                job['days_old'] = days_old

        # Apply Python-side filters (source, industry, origin are computed, not stored)
        if source:
            job_list = [j for j in job_list if j['source'] == source]
        if industry_filter:
            job_list = [j for j in job_list if j['industry'] == industry_filter]
        if origin_filter in ('email', 'search'):
            job_list = [j for j in job_list if j['origin'] == origin_filter]
        if inbox_filter:
            job_list = [j for j in job_list if j['email_account'] == inbox_filter]

        # Sort based on selected option
        if sort_by == 'date':
            job_list.sort(key=lambda x: x['date'], reverse=True)
        elif sort_by == 'salary':
            # Sort by salary (highest first), handle missing salaries
            job_list.sort(key=lambda x: (x['salary'] is None, x['salary']), reverse=True)
        else:  # default: score
            job_list.sort(key=lambda x: x['score'], reverse=True)

        # Get unique email inboxes (accounts that have tracked jobs)
        cursor.execute("SELECT DISTINCT source FROM jobs WHERE source LIKE 'Email%'")
        inboxes = []
        for r in cursor.fetchall():
            src = r[0] or ''
            if '/' in src:
                addr = src.split('/', 1)[1].strip()
                username = addr.split('@')[0]
                if username and username not in [i['username'] for i in inboxes]:
                    inboxes.append({'username': username, 'full': addr})

        # Get unique sources (derived from URLs, not the raw "JSearch" source field)
        cursor.execute("SELECT DISTINCT link FROM jobs WHERE link IS NOT NULL")
        raw_links = [row[0] for row in cursor.fetchall()]
        sources = sorted(set(extract_source_from_url(link) for link in raw_links))

        # Get unique dates (most recent first)
        cursor.execute("SELECT DISTINCT DATE(extracted_date) as d FROM jobs ORDER BY d DESC")
        dates = [row[0] for row in cursor.fetchall() if row[0]]

        # Get last sync time
        cursor.execute("SELECT MAX(extracted_date) FROM jobs")
        last_sync_raw = cursor.fetchone()[0]
        last_sync = last_sync_raw if last_sync_raw else None

        # Count cover letters
        cover_letter_count = len(covered_job_ids)

        # Count application statuses
        status_counts = {'applied': 0, 'interviewing': 0, 'rejected': 0}
        for status in status_map.values():
            if status in status_counts:
                status_counts[status] += 1

        conn.close()

    except Exception as e:
        job_list = []
        sources = []
        dates = []
        last_sync = None
        cover_letter_count = 0
        status_counts = {'applied': 0, 'interviewing': 0, 'rejected': 0}
        print(f"DB Error: {e}")

    return render_template('dashboard.html',
                         jobs=job_list,
                         sources=sources,
                         dates=dates,
                         industries=list(INDUSTRIES.keys()),
                         inboxes=inboxes,
                         selected_source=source,
                         selected_date=date_filter,
                         selected_industry=industry_filter,
                         selected_origin=origin_filter,
                         selected_inbox=inbox_filter,
                         min_score=min_score,
                         last_sync=last_sync,
                         total_jobs=len(job_list),
                         cover_letter_count=cover_letter_count,
                         sort_by=sort_by,
                         status_counts=status_counts)

@app.route('/api/export-csv')
def export_csv():
    """Export all current jobs as a CSV download."""
    import csv
    import io
    from flask import Response

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, job_title, company, link, salary, source,
               DATE(extracted_date) as date, relevance_score, best_cv,
               COALESCE(seen_count, 1) as seen_count
        FROM jobs ORDER BY relevance_score DESC
    """)
    rows = cursor.fetchall()

    # Application statuses
    cursor.execute("SELECT job_id, status FROM application_status")
    status_map = {r[0]: r[1] for r in cursor.fetchall()}
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Title', 'Company', 'Link', 'Salary', 'Source',
                     'Date', 'Score %', 'Best CV', 'Seen Count', 'Status'])
    for row in rows:
        job_id = row[0]
        cv_label = format_cv_label(row[8]) or ''
        writer.writerow([
            job_id, row[1], row[2], row[3], row[4] or '', row[5] or '',
            row[6], round(row[7] or 0, 1), cv_label, row[9],
            status_map.get(job_id, 'none')
        ])

    output.seek(0)
    date_str = datetime.now().strftime('%Y-%m-%d')
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=jobs_{date_str}.csv'}
    )

@app.route('/api/similar-jobs/<int:job_id>')
def similar_jobs(job_id):
    """Return jobs similar to the given job_id based on title keywords."""
    conn = get_db()
    cursor = conn.cursor()

    # Get the reference job
    cursor.execute("SELECT job_title, description FROM jobs WHERE id = ?", (job_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return jsonify([])

    title, desc = row
    # Extract meaningful words from title (3+ chars, not stopwords)
    stopwords = {'the', 'and', 'for', 'with', 'remote', 'senior', 'junior',
                 'lead', 'staff', 'principal', 'from', 'home'}
    title_words = [w.lower() for w in (title or '').split()
                   if len(w) > 3 and w.lower() not in stopwords]

    if not title_words:
        conn.close()
        return jsonify([])

    # Find jobs where title contains at least one matching word
    conditions = ' OR '.join(['LOWER(job_title) LIKE ?' for _ in title_words])
    params = [f'%{w}%' for w in title_words] + [job_id]
    cursor.execute(f"""
        SELECT id, job_title, company, relevance_score, link, best_cv
        FROM jobs
        WHERE ({conditions}) AND id != ?
        ORDER BY relevance_score DESC
        LIMIT 8
    """, params)

    results = []
    for r in cursor.fetchall():
        cv_label = format_cv_label(r[5]) or ''
        results.append({
            'id': r[0], 'title': r[1], 'company': r[2],
            'score': round(r[3] or 0, 1), 'link': r[4], 'cv': cv_label
        })
    conn.close()
    return jsonify(results)

@app.route('/api/job-delete', methods=['POST'])
def delete_job():
    """Remove a job from the database (dead link / irrelevant)."""
    data = request.get_json()
    job_id = data.get('job_id')
    if not job_id:
        return jsonify({'error': 'Missing job_id'}), 400
    try:
        conn = get_db()
        conn.execute('DELETE FROM jobs WHERE id = ?', (job_id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/job-status', methods=['POST'])
def update_job_status():
    """Update application status for a job."""
    init_application_status_table()
    data = request.get_json()
    job_id = data.get('job_id')
    status = data.get('status')

    if not job_id or status not in ['none', 'applied', 'interviewing', 'rejected']:
        return jsonify({'error': 'Invalid request'}), 400

    try:
        conn = get_db()
        cursor = conn.cursor()

        # Upsert status
        cursor.execute("""
            INSERT INTO application_status (job_id, status)
            VALUES (?, ?)
            ON CONFLICT(job_id) DO UPDATE SET status = excluded.status, updated_date = CURRENT_TIMESTAMP
        """, (job_id, status))

        conn.commit()
        conn.close()
        return jsonify({'success': True, 'status': status})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/stats')
def stats():
    """Dashboard stats."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM jobs")
        total_jobs = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(DISTINCT source) FROM jobs")
        total_sources = cursor.fetchone()[0]
        cursor.execute("SELECT AVG(relevance_score) FROM jobs")
        avg_score = cursor.fetchone()[0]
        cursor.execute("SELECT MAX(relevance_score) FROM jobs")
        max_score = cursor.fetchone()[0]
        conn.close()
        return {
            'total_jobs': total_jobs,
            'total_sources': total_sources,
            'avg_score': round(avg_score, 1) if avg_score else 0,
            'max_score': round(max_score, 1) if max_score else 0
        }
    except:
        return {}

if __name__ == '__main__':
    print("\n" + "="*70)
    print("JOB DASHBOARD")
    print("="*70)
    print("\nStarting web server...")
    print("Open your browser to: http://localhost:5000")
    print("Press Ctrl+C to stop\n")

    app.run(debug=True, port=5000)
