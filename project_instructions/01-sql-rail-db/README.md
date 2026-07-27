# 🚉 RailPulse: Belgian Transit SQL Analysis

- **Repository:** `railpulse_sql_analysis`
- **Type of Challenge:** `Learning`
- **Duration:** `4 days`
- **Deadline:** `24/07/2026 5:00 PM`
- **Team challenge:** `Team (in spirit)`
- **Presentations:** During team feedback every learner will answer a random question from the team study guide.

## Mission objectives

Consolidate your knowledge in SQL, specifically in:
- `JOIN` operations (Cross-referencing stations, types, and operational records)
- `GROUP BY` and `AGGREGATION` operations (Averages, counts, sums of transit delays)
- Time-series manipulations (`DATETIME` extractions in SQLite)
- Data ingestion and normalization patterns (Transforming API JSON into relational tables)

## Learning Objectives

- To be able to design and build an SQL database schema from API data structure.
- To be able to query relational tables to extract performance metrics.
- To be able to handle date/time objects and calculate delays in SQL.
- To be able to present an operational efficiency analysis to a business client.

## The Mission

> We are *RailPulse*, an urban mobility consulting firm. The Belgian National Railway company (SNCB/NMBS) wants a clear overview of operational performance and delay patterns to optimize their winter scheduling. Your mission is to extract liveboard data directly from the iRail API, build a normalized database, and provide an analytical report detailing network bottlenecks.

## The Data & Architecture

You will build the database from scratch using the SNBC static data portal. Please create an free account for access to the developer portal: [https://data.belgianmobility.io/en/data.html](https://data.belgianmobility.io/en/data.html).


You must map out your schema in [SQLite](https://www.sqlite.org/index.html) format. Your schema should neatly normalize each relevant table using strict primary and foreign keys.

⚠️ **Note:** Be mindful of the request limits so you don't get blocked! Read the documentation for the usage of the API to make sure your compliant.

---

## Must have features
- You must have an SQLite database and a diagram with the appropiate schema (hint: use [https://www.drawdb.app/](https://www.drawdb.app/))
- Each table in your database must contain clean data
- You must answer the questions below using SQL queries only!

**Key questions for analysis:**

1. **The Peak Hour Problem:** What hour of the day experiences the highest volume of scheduled train departures across the entire network?
2. **Platform Bottlenecks:** Identify the top 3 busiest platforms in Brussels-Central. 
3. **Busiest Morning Destinations**: Find the top 3 most frequent terminal destinations (trip_headsign) for all morning trips that depart before 12:00:00 PM.
4. **Service Frequency**: Classify each active service ID into a weekly frequency category using a CASE WHEN statement. If a service operates 5 or more days a week, classify it as "High Frequency"; if 2–4 days, "Medium Frequency"; and if 1 day or completely irregular, "Low Frequency/Special". Show the percentage of services in each category.
5. **The Accessibility Audit (Vehicle Features):** Calculate the exact ratio and percentage of scheduled trips per route that explicitly guarantee wheelchair accessibility or bicycle storage (bikes_allowed). Which specific routes score the lowest in passenger amenity availability?

---

### Nice-to-have features 

Ready to take your analytics further? 
- **Live Stream Integration:** Can you implement a simple loop mechanism or cron job simulation to pull and append data to your SQLite db every day to build a deeper historical timeline? (No worries if not, we will do it in the next sprint!) You can use the Real-time API in this case. 
- **Network Leaderboard:** Create a visual leaderboard comparing these 5 main hubs. Which city has the most efficient, on-time station?
- **Index Optimization:** Run an `EXPLAIN QUERY PLAN` on your heaviest query. Implement appropriate SQL `INDEX` structures on columns like `station_id` or `scheduled_time` to prove you can speed up lookups.

---

### Constraints

- You are **not** allowed to use `pandas` or similar data-frame engines to filter or aggregate data. Python must *only* be used for the network `requests` and executing raw SQL via `sqlite3`.
- All analytical questions must be solved using standard SQL operations (`JOIN`, `GROUP BY`, `HAVING`).
- Write your table definitions and analytics queries in dedicated, clean `.sql` files.
- (optional) You may use us `pandas` as a way to visualize the data or to insert data into your database. 

## Deliverables

1. Publish your source code on a GitHub repository.
2. Pimp up the README file:
   - Project Description
   - Entity Relationship Diagram
   - (Visuals)
   - (Contributors)
   - (Timeline)
   - (Personal situation)
3. Team Feedback: Give a short overview (5 minutes max) of your approach and database design, along with the answers to the key analytical questions. In Q&A, Each learner will be asked a question from the technical interview check.  

### Steps

1. Create the repository.
2. Read the SNCB documentation and design your schema.
3. Write the ingestion script to create the tables and fetch data for Brussels-Central.
4. Scale up the data fetcher if aiming for the nice-to-haves.
5. Query the SQLite database to answer the core operational questions.
6. Create an analytical dashboard (via Streamlit or your presentation tool) using your clean SQL outputs.

## Evaluation criteria

| Criteria       | Indicator                                              | Yes/No |
| -------------- | ------------------------------------------------------ | ------ |
| 1. Is complete | Database tables are correctly normalized with Foreign Keys      |        |
|                | All 5 core analytical questions are accurately answered|        |
|                | Visualization components match the data queries        |        |
| 2. Is great    | SQL queries use performance indexes effectively         |        |
|                | Multiple major hubs are successfully analyzed           |        |
|                | Report provides actionable insights on delay patterns |        |


## 📑 Technical Interview Check: SQL&DB_theory.md

There are some fundamental topics regarding databases and SQL we might not see in this project. As a team create a markdown file called, `SQL&DB_theory.md` covering the topics below. Ideally, you take a moment to discuss and research the answers together. During presentations, each learner will be asked a question on the topics at random! (Tip: Add images and any visual resources). I will compile all the files and add them on Moodle for future interview prep. Feel free to add anything else you think  is helpful as a study guide!.

1. General Database Paradigms
- What's the difference between an SQL and a NoSQL database?
- What other types of database engines exist (e.g., Graph, Vector, Time-Series), and how does SQLite compare to them?
- If our iRail pipeline grows to have 50 separate scraper scripts trying to insert train records simultaneously, why would our current SQLite setup fail, and which engine should we migrate to?

2. Relational Schema & Data Modeling
- What is a data model in the context of databases?
- What is the difference between a one-to-one, many-to-one, and many-to-many relationship? Give an example using Stations, Vehicles, and Departures.
- What is database normalization? Why is it important? Is your project database normalized? How do you know?
- What is the physical and logical difference between a Primary Key, a Foreign Key, and a Unique Key?

3. Analytical Modeling & Architecture
- What is a fact vs. a dimension table? If you were building a data warehouse for RailPulse, which category would the departures table fall into?
- What is the difference between a Star Schema and a Snowflake Schema in data warehousing?

4. Theoretical Frameworks & Guardrails
- What is the ACID framework? Give an example of a database transaction failure using this project's data ingestion.

- What is the CAP theorem? If the iRail network API goes offline during a live scrape, how does a SQL database handle the trade-off between Consistency and Availability?

5. Database Objects & Query Mechanisms
- What is the difference between a View, a Window function, and a Subquery? When would you choose one over the others?

- What is the difference between an Index Scan and an Index Seek? Which one is faster and why?

- What is a "SARGable" violation in a WHERE clause, and how does writing a function like WHERE strftime('%Y', scheduled_time) = '2026' degrade query performance?

6. Advanced Query Optimization & Performance Tuning
- A developer wrote a query that joins a table to an aggregated subquery:

```SQL
FROM orders 
JOIN (SELECT customer_id, MAX(date) FROM logs GROUP BY customer_id) AS sub 
  ON orders.customer_id = sub.customer_id
```
How could this impact server memory/performance, and what is the optimized alternative (e.g., Temporary Tables or Window Functions)?

## A final note of encouragement

![Train operating in winter condition](https://media4.giphy.com/media/v1.Y2lkPTc5MGI3NjExdHl1ZmgwdzB2a200Z3l5ZGo1aXhtNGs0Z2l2ZGhxeXZlYjY0ODhzMyZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/UUpG9wI86iLD2/giphy.gif)
