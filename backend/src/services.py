from .db import query, execute


def audit(user, action, entity_type, entity_id, detail):
    execute("INSERT INTO audit_logs(actor_email,action,entity_type,entity_id,detail) VALUES(%s,%s,%s,%s,%s)", (user["email"], action, entity_type, entity_id, detail))

def survey_rows():
    return query("""
      SELECT s.*, t.full_name AS tenant_name, t.status AS tenant_status, t.property_id, t.lease_end_date,
             p.code AS property_code, p.name AS property_name, p.monthly_rent,
             p.property_age_years,
             COALESCE(AVG(se.resolution_days) FILTER (WHERE se.status='resolved'),0) AS service_resolution_days,
             CASE WHEN p.monthly_rent > 0 THEN p.monthly_rent/5000.0 ELSE 0 END AS rent_burden
      FROM renewal_surveys s
      JOIN tenants t ON t.id=s.tenant_id
      JOIN properties p ON p.id=t.property_id
      LEFT JOIN service_events se ON se.tenant_id=t.id
      GROUP BY s.id,t.id,p.id
      ORDER BY s.collected_at DESC
    """)
