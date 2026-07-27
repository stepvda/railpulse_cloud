# 🚉 RailPulse Cloud: Azure Challenge

- **Repository:** `railpulse-challenge-azure`
- **Type of Challenge:** `Learning`
- **Duration:** `5 days`
- **Deadline:** `31/07/26 5:00 PM`
- **Team challenge:** `Team (in spirit)`

## Mission 

> We are *RailPulse*, an urban mobility consulting firm. The Belgian National Railway company (SNCB/NMBS) wants to transition their legacy on-premise delay reporting into a modern, cloud-native architecture. Your mission is to build an automated ETL pipeline that pulls real-time liveboard metrics from the SNCB API, processes them in a serverless environment, and pipes them directly into Azure SQL so it's perfectly structured for next week's analytics dashboards.

Now that you have mastered raw SQL queries and data normalization locally, it is time to move your data pipeline infrastructure to the cloud. 

Goal: Build the secure, scalable, cloud-native **Data Warehouse** that you will directly connect to and visualize next week. 

## Learning Objectives

- To be able to read an open API schema and map it to a cloud relational database.
- To be able to deploy an operational serverless function via the Azure Portal.
- To be able to configure basic networking and security constraints in Microsoft Azure.
- To be able to keep a cloud project free and optimized under budgetary constraints.
- To be able to prepare a robust database foundation optimized for a future BI tool connection.

## The Azure Setup (Stay 100% Free!)

You will be provided with a `@becode.education` Microsoft account. Use it to log into the [Azure Portal](https://portal.azure.com/#home) and activate **Azure for Students**. This gives you **$100 in free credits** without requiring a credit card.

> ### ⚠️ Critical: Cost-Control Settings
> To ensure your subscription stays entirely free and your $100 credit lasts the whole year, apply these strict configurations when creating your resources:
> * **Resource Group:** Place ALL resources for this challenge into a single, dedicated Resource Group so you can delete/pause everything easily on Friday.
> * **Azure SQL Database:** >   * Compute + Storage: Select **Serverless** (not Provisioned).
>   * Set the **Auto-pause delay** to exactly **1 hour** (this turns off the DB when you aren't querying it so it stops eating credits).
>   * Shrink the max size down to **2 GB** or less.
> * **Azure Function App:** Set the Hosting Plan to **Consumption (Serverless)**. You only pay for the exact milliseconds your code executes.
> * **Storage Account:** Select **Locally Redundant Storage (LRS)**. Avoid Geo-Redundant options as they cost triple.


## Must-Have: Azure Function Pipeline via Portal

### Objective  
Use the Azure web portal interface to deploy a Python Azure Function that fetches live train data and logs it cleanly into an Azure SQL Database. This should follow neatly, from your previous project! 

### Steps
1. **Provision Azure SQL:** Create a serverless database. Adjust the firewall settings to allow your local machine's IP address and check the option to *“Allow Azure services and resources to access this server”*.
2. **Build the Database Schema:** Write a script to create your tables (e.g., `stations`, `vehicles`, `liveboard_records`) with appropriate data types (`VARCHAR`, `INT`, `DATETIME`). Ensure you incorporate the relationships learned in your SQL sprint.
3. **Deploy the Function App:** Create a Python 3.10+ Function App on a Consumption plan. Write an HTTP-triggered function that hits the SNCB endpoints for a major hub (like Brussels-Central).
4. **Environment Security:** Never hardcode passwords. Save your SQL connection string inside the Function App's **Environment variables** (Application Settings) and access it via `os.environ`.

### Deliverables
* ✅ A live, operational Azure Function App HTTP endpoint.
* ✅ An Azure SQL Database containing cleanly populated, normalized tables.
* ✅ A comprehensive README detailing your database schema choice.

---

## Nice-to-Have: Automation, Scaling, and Scheduling

### Objective  
Turn your manual pipeline into a fully automated cloud monitoring tool, gathering the historical depth required for high-quality charts.

### Additions
1. **Automated Scheduling:** Transition your code or create a second function using a **Timer Trigger** (CRON job configuration) to automatically pull liveboard data every 15 or 30 minutes. 
2. **Idempotency Logic:** Ensure your SQL queries handle duplicates gracefully (e.g., using `INSERT OR IGNORE` or checking if a record exists before writing) so recurring timer runs don't corrupt your dataset.
3. **Multi-Hub Expansion:** Scale your ingestion script to poll data from multiple key stations across Belgium (e.g., Antwerpen-Centraal, Gent-Sint-Pieters, Liège-Guillemins) to create a more comprehensive dataset for next week's dashboard.

---

## 📝 Evaluation Criteria

| Category | Must-Have | Nice-to-Have | 
| :--- | :--- | :--- | 
| **Function App Deployment** | Manual Portal Setup | Optimized Timer trigger |
| **SQL DB Population** | Contains core data |  Deduplication logic built | 
| **Cost Optimization** |  Serverless configurations | DB Auto-pause configured |  
| **BI Readiness** | Basic tables generated |  Historical data via timer |  
| **Code Execution** | Raw script blocks | Clean modular functions |

## A final word of encouragement
"Sometimes that light at the end of the tunnel is a train." - Charles Barkley

![train_in_cloud](https://i.pinimg.com/736x/29/52/13/295213a0c4ef5e9ff7d860e3d02729c4.jpg)
