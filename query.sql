SELECT p.name AS 姓名, n.note AS 备注
FROM people p JOIN notes n ON p.id=n.person_id ORDER BY p.id
