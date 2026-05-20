--
-- ------------------------------------------------------

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!50503 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

--
-- Table structure for table `activity_enrollment`
--

DROP TABLE IF EXISTS `activity_enrollment`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `activity_enrollment` (
  `id` int NOT NULL AUTO_INCREMENT,
  `target_phone` varchar(50) NOT NULL,
  `target_uuid` varchar(64) DEFAULT NULL,
  `enrolled_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `enrolled_by` varchar(255) DEFAULT NULL,
  `notes` text,
  `active` tinyint(1) NOT NULL DEFAULT '1',
  `error_backoff_until` datetime DEFAULT NULL,
  `consecutive_errors` int NOT NULL DEFAULT '0',
  `platform` varchar(16) NOT NULL DEFAULT 'signal',
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_ae_phone` (`target_phone`),
  KEY `idx_ae_active` (`active`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `activity_probes`
--

DROP TABLE IF EXISTS `activity_probes`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `activity_probes` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `target_phone` varchar(50) NOT NULL,
  `target_uuid` varchar(64) DEFAULT NULL,
  `group_id` varchar(128) NOT NULL,
  `target_author_phone` varchar(50) NOT NULL,
  `target_sent_ts_ms` bigint NOT NULL,
  `probe_sent_ms` bigint NOT NULL,
  `emoji` varchar(32) NOT NULL,
  `removed` tinyint(1) NOT NULL DEFAULT '0',
  `status` enum('pending','acked','timeout','error') NOT NULL DEFAULT 'pending',
  `error_msg` text,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `platform` varchar(16) NOT NULL DEFAULT 'signal',
  PRIMARY KEY (`id`),
  KEY `idx_ap_uuid_status` (`target_uuid`,`status`,`probe_sent_ms`),
  KEY `idx_ap_phone_time` (`target_phone`,`probe_sent_ms`),
  KEY `idx_ap_status_time` (`status`,`probe_sent_ms`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `activity_samples`
--

DROP TABLE IF EXISTS `activity_samples`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `activity_samples` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `probe_id` bigint DEFAULT NULL,
  `target_phone` varchar(50) NOT NULL,
  `target_uuid` varchar(64) DEFAULT NULL,
  `source_device` int DEFAULT NULL,
  `receipt_type` varchar(16) DEFAULT NULL,
  `rtt_ms` int DEFAULT NULL,
  `state` enum('active','standby','offline','extra_device_receipt','error') NOT NULL,
  `median_ms_used` int DEFAULT NULL,
  `observed_at` datetime NOT NULL,
  `platform` varchar(16) NOT NULL DEFAULT 'signal',
  PRIMARY KEY (`id`),
  KEY `idx_as_phone_time` (`target_phone`,`observed_at`),
  KEY `idx_as_probe` (`probe_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `app_settings`
--

DROP TABLE IF EXISTS `app_settings`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `app_settings` (
  `setting_key` varchar(128) NOT NULL,
  `setting_value` text,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`setting_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `attachments`
--

DROP TABLE IF EXISTS `attachments`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `attachments` (
  `id` int NOT NULL AUTO_INCREMENT,
  `file_name` varchar(255) NOT NULL,
  `file_content` longblob,
  `base64_content` longtext,
  `md5sum` varchar(32) DEFAULT NULL,
  `created_timestamp` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `platform` varchar(16) NOT NULL DEFAULT 'signal',
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_md5sum` (`md5sum`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `chats`
--

DROP TABLE IF EXISTS `chats`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `chats` (
  `id` int NOT NULL AUTO_INCREMENT,
  `platform` varchar(16) NOT NULL,
  `platform_chat_id` varchar(190) NOT NULL,
  `connector_id` varchar(64) DEFAULT NULL,
  `title` varchar(255) DEFAULT NULL,
  `kind` enum('group','channel','dm') DEFAULT 'group',
  `is_public` tinyint(1) DEFAULT '0',
  `member_count` int DEFAULT '0',
  `first_seen_at` datetime DEFAULT NULL,
  `last_seen_at` datetime DEFAULT NULL,
  `is_monitored` tinyint(1) DEFAULT '1',
  `raw_meta` json DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_chat` (`platform`,`platform_chat_id`(120)),
  KEY `idx_chat_platform` (`platform`),
  KEY `idx_chat_title` (`title`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `connector_cursors`
--

DROP TABLE IF EXISTS `connector_cursors`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `connector_cursors` (
  `connector_id` varchar(64) NOT NULL,
  `cursor` varchar(190) DEFAULT NULL,
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`connector_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `daily_summaries`
--

DROP TABLE IF EXISTS `daily_summaries`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `daily_summaries` (
  `id` int NOT NULL AUTO_INCREMENT,
  `summary_date` date NOT NULL,
  `group_name` varchar(255) NOT NULL,
  `summary_text` longtext NOT NULL,
  `model_used` varchar(128) DEFAULT NULL,
  `char_count` int DEFAULT NULL,
  `message_count` int DEFAULT NULL,
  `generated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `platform` varchar(16) NOT NULL DEFAULT 'signal',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_daily_date_group` (`summary_date`,`group_name`),
  KEY `idx_daily_date` (`summary_date`),
  KEY `idx_daily_group_date` (`group_name`,`summary_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `group_members`
--

DROP TABLE IF EXISTS `group_members`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `group_members` (
  `group_id` varchar(128) NOT NULL,
  `member_phone` varchar(50) DEFAULT NULL,
  `member_uuid` varchar(64) DEFAULT NULL,
  `role` enum('member','admin') DEFAULT 'member',
  `first_seen_at` datetime NOT NULL,
  `last_seen_at` datetime NOT NULL,
  `left_at` datetime DEFAULT NULL,
  `identity_key` varchar(64) GENERATED ALWAYS AS (coalesce(`member_phone`,`member_uuid`)) STORED NOT NULL,
  `platform` varchar(16) NOT NULL DEFAULT 'signal',
  PRIMARY KEY (`group_id`,`identity_key`),
  KEY `idx_gm_uuid` (`member_uuid`),
  KEY `idx_gm_left` (`left_at`),
  KEY `idx_gm_role` (`role`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `group_membership_events`
--

DROP TABLE IF EXISTS `group_membership_events`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `group_membership_events` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `group_id` varchar(128) NOT NULL,
  `group_name` varchar(255) DEFAULT NULL,
  `member_phone` varchar(50) DEFAULT NULL,
  `member_uuid` varchar(64) DEFAULT NULL,
  `event_type` enum('join','leave','admin_grant','admin_revoke','invite_added','invite_removed','request_added','request_approved','name_change','description_change','invite_link_change') NOT NULL,
  `detail` text,
  `detected_at` datetime NOT NULL,
  `platform` varchar(16) NOT NULL DEFAULT 'signal',
  PRIMARY KEY (`id`),
  KEY `idx_gme_group_time` (`group_id`,`detected_at`),
  KEY `idx_gme_member` (`member_phone`),
  KEY `idx_gme_type` (`event_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `group_snapshots`
--

DROP TABLE IF EXISTS `group_snapshots`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `group_snapshots` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `group_id` varchar(128) NOT NULL,
  `snapshot_at` datetime NOT NULL,
  `name` varchar(255) DEFAULT NULL,
  `description` text,
  `invite_link` varchar(500) DEFAULT NULL,
  `internal_id` varchar(255) DEFAULT NULL,
  `member_count` int DEFAULT '0',
  `admin_count` int DEFAULT '0',
  `pending_invites_count` int DEFAULT '0',
  `pending_requests_count` int DEFAULT '0',
  `blocked` tinyint(1) DEFAULT '0',
  `raw_json` json DEFAULT NULL,
  `platform` varchar(16) NOT NULL DEFAULT 'signal',
  PRIMARY KEY (`id`),
  KEY `idx_gs_group_time` (`group_id`,`snapshot_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `identities`
--

DROP TABLE IF EXISTS `identities`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `identities` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `label` varchar(255) DEFAULT NULL,
  `notes` text,
  `is_confirmed` tinyint(1) DEFAULT '0',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `identity_links`
--

DROP TABLE IF EXISTS `identity_links`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `identity_links` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `identity_id` bigint NOT NULL,
  `platform` varchar(16) NOT NULL,
  `platform_user_id` varchar(190) NOT NULL,
  `link_method` enum('manual','phone_exact','username_exact','displayname_fuzzy','url_cooccurrence','behavioral','reply_pattern') NOT NULL,
  `confidence` float DEFAULT '0',
  `evidence` json DEFAULT NULL,
  `status` enum('proposed','confirmed','rejected') DEFAULT 'proposed',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_link` (`platform`,`platform_user_id`(120),`identity_id`),
  KEY `idx_il_identity` (`identity_id`),
  KEY `idx_il_status` (`status`,`confidence`),
  KEY `idx_il_account` (`platform`,`platform_user_id`(120))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `intel_briefs`
--

DROP TABLE IF EXISTS `intel_briefs`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `intel_briefs` (
  `id` int NOT NULL AUTO_INCREMENT,
  `brief_date` date NOT NULL,
  `content` longtext,
  `status` enum('pending','generating','done','error') DEFAULT 'pending',
  `error_msg` text,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `completed_at` datetime DEFAULT NULL,
  `platform` varchar(16) NOT NULL DEFAULT 'signal',
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_ib_date` (`brief_date`),
  KEY `idx_ib_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `keyword_watchlist`
--

DROP TABLE IF EXISTS `keyword_watchlist`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `keyword_watchlist` (
  `id` int NOT NULL AUTO_INCREMENT,
  `keyword` varchar(255) NOT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `last_triggered` datetime DEFAULT NULL,
  `trigger_count` int DEFAULT '0',
  `is_active` tinyint(1) DEFAULT '1',
  PRIMARY KEY (`id`),
  UNIQUE KEY `keyword` (`keyword`),
  KEY `idx_kw_active` (`is_active`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `llm_tasks`
--

DROP TABLE IF EXISTS `llm_tasks`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `llm_tasks` (
  `id` int NOT NULL AUTO_INCREMENT,
  `task_type` varchar(50) NOT NULL,
  `task_key` varchar(255) NOT NULL,
  `status` enum('pending','running','done','error') NOT NULL DEFAULT 'pending',
  `priority` int NOT NULL DEFAULT '5',
  `input_data` longtext,
  `result` longtext,
  `error_msg` text,
  `attempts` int NOT NULL DEFAULT '0',
  `max_attempts` int NOT NULL DEFAULT '3',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `started_at` datetime DEFAULT NULL,
  `completed_at` datetime DEFAULT NULL,
  `expires_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_status_priority` (`status`,`priority`,`created_at`),
  KEY `idx_type_key` (`task_type`,`task_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `message_attachments`
--

DROP TABLE IF EXISTS `message_attachments`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `message_attachments` (
  `id` int NOT NULL AUTO_INCREMENT,
  `message_id` int NOT NULL,
  `attachment_id` varchar(255) NOT NULL,
  `file_name` varchar(512) DEFAULT NULL,
  `content_type` varchar(128) DEFAULT NULL,
  `size_bytes` bigint DEFAULT NULL,
  `sender_name` varchar(255) DEFAULT NULL,
  `sender_phone` varchar(64) DEFAULT NULL,
  `group_name` varchar(255) DEFAULT NULL,
  `group_id` varchar(128) DEFAULT NULL,
  `sent_timestamp` datetime DEFAULT NULL,
  `platform` varchar(16) NOT NULL DEFAULT 'signal',
  `ai_caption` text,
  `caption_status` varchar(16) DEFAULT NULL,
  `caption_model` varchar(64) DEFAULT NULL,
  `captioned_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_ma_attachment_id` (`attachment_id`),
  KEY `idx_ma_file_name` (`file_name`),
  KEY `idx_ma_message_id` (`message_id`),
  KEY `idx_ma_sender` (`sender_name`),
  KEY `idx_ma_group` (`group_name`),
  KEY `idx_ma_caption_status` (`caption_status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `message_entities`
--

DROP TABLE IF EXISTS `message_entities`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `message_entities` (
  `id` int NOT NULL AUTO_INCREMENT,
  `message_id` int NOT NULL,
  `entity_text` varchar(255) NOT NULL,
  `entity_type` enum('person','organization','location','event','other') DEFAULT 'other',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `platform` varchar(16) NOT NULL DEFAULT 'signal',
  PRIMARY KEY (`id`),
  KEY `idx_me_message` (`message_id`),
  KEY `idx_me_entity` (`entity_text`(100)),
  KEY `idx_me_type` (`entity_type`),
  KEY `idx_me_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `message_mentions`
--

DROP TABLE IF EXISTS `message_mentions`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `message_mentions` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `message_id` int NOT NULL,
  `mentioned_phone` varchar(50) DEFAULT NULL,
  `mentioned_uuid` varchar(64) DEFAULT NULL,
  `mention_start` int DEFAULT NULL,
  `mention_length` int DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `platform` varchar(16) NOT NULL DEFAULT 'signal',
  PRIMARY KEY (`id`),
  KEY `idx_mm_msg` (`message_id`),
  KEY `idx_mm_target` (`mentioned_phone`),
  KEY `idx_mm_uuid` (`mentioned_uuid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `message_quotes`
--

DROP TABLE IF EXISTS `message_quotes`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `message_quotes` (
  `message_id` int NOT NULL,
  `quoted_author_phone` varchar(50) DEFAULT NULL,
  `quoted_author_uuid` varchar(64) DEFAULT NULL,
  `quoted_sent_ts` bigint DEFAULT NULL,
  `quoted_text` text,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `platform` varchar(16) NOT NULL DEFAULT 'signal',
  PRIMARY KEY (`message_id`),
  KEY `idx_mq_author_ts` (`quoted_author_phone`,`quoted_sent_ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `messages`
--

DROP TABLE IF EXISTS `messages`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `messages` (
  `id` int NOT NULL AUTO_INCREMENT,
  `sender_name` varchar(255) DEFAULT NULL,
  `sender_phone` varchar(50) DEFAULT NULL,
  `message` text,
  `url` varchar(2083) DEFAULT NULL,
  `group_name` varchar(255) DEFAULT NULL,
  `group_id` varchar(255) DEFAULT NULL,
  `sent_timestamp` timestamp NULL DEFAULT NULL,
  `ai-analysis` text,
  `screenshot` longblob,
  `sentiment` varchar(20) DEFAULT NULL,
  `source_uuid` varchar(64) DEFAULT NULL,
  `source_device` smallint DEFAULT NULL,
  `server_received_ts` datetime(3) DEFAULT NULL,
  `server_delivered_ts` datetime(3) DEFAULT NULL,
  `expires_in_seconds` int DEFAULT NULL,
  `raw_envelope` json DEFAULT NULL,
  `message_type` varchar(24) DEFAULT 'message',
  `deleted_at` datetime(3) DEFAULT NULL,
  `platform` varchar(16) NOT NULL DEFAULT 'signal',
  `connector_id` varchar(64) DEFAULT NULL,
  `platform_chat_id` varchar(190) DEFAULT NULL,
  `platform_msg_id` varchar(190) DEFAULT NULL,
  `platform_user_id` varchar(190) DEFAULT NULL,
  `sender_username` varchar(190) DEFAULT NULL,
  `edited_at` datetime(3) DEFAULT NULL,
  `account_key` varchar(190) GENERATED ALWAYS AS (coalesce(`platform_user_id`,`sender_phone`)) STORED,
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_msg_platform_dedup` (`platform`,`platform_chat_id`(80),`platform_msg_id`(100),`platform_user_id`(64)),
  UNIQUE KEY `idx_msg_dedup` (`sender_phone`(20),`group_id`(64),`sent_timestamp`),
  KEY `idx_msg_sent_ts` (`sent_timestamp`),
  KEY `idx_msg_group` (`group_name`),
  KEY `idx_msg_sender` (`sender_name`),
  KEY `idx_msg_sentiment` (`sentiment`),
  KEY `idx_msg_source_uuid` (`source_uuid`),
  KEY `idx_msg_source_device` (`source_device`),
  KEY `idx_msg_server_ts` (`server_received_ts`),
  KEY `idx_msg_type` (`message_type`),
  KEY `idx_msg_deleted` (`deleted_at`),
  KEY `idx_msg_platform` (`platform`,`sent_timestamp`),
  KEY `idx_msg_platform_chat` (`platform`,`platform_chat_id`(80)),
  KEY `idx_msg_platform_user` (`platform`,`platform_user_id`(64)),
  KEY `idx_msg_account_key` (`platform`,`account_key`(120)),
  FULLTEXT KEY `idx_ft_search` (`message`,`ai-analysis`,`url`,`sender_name`,`group_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `monthly_summaries`
--

DROP TABLE IF EXISTS `monthly_summaries`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `monthly_summaries` (
  `id` int NOT NULL AUTO_INCREMENT,
  `month_start` date NOT NULL,
  `group_name` varchar(255) NOT NULL,
  `summary_text` longtext NOT NULL,
  `daily_count` int NOT NULL DEFAULT '0',
  `model_used` varchar(128) DEFAULT NULL,
  `generated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `platform` varchar(16) NOT NULL DEFAULT 'signal',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_month_group` (`month_start`,`group_name`),
  KEY `idx_month` (`month_start`),
  KEY `idx_month_group` (`group_name`,`month_start`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `page_changes`
--

DROP TABLE IF EXISTS `page_changes`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `page_changes` (
  `id` int NOT NULL AUTO_INCREMENT,
  `url` varchar(2083) NOT NULL,
  `snapshot_old_id` int DEFAULT NULL,
  `snapshot_new_id` int DEFAULT NULL,
  `change_pct` float DEFAULT NULL,
  `detected_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_pc_url` (`url`(255)),
  KEY `idx_pc_detected` (`detected_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `page_snapshots`
--

DROP TABLE IF EXISTS `page_snapshots`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `page_snapshots` (
  `id` int NOT NULL AUTO_INCREMENT,
  `url` varchar(2083) NOT NULL,
  `html_content` longtext NOT NULL,
  `captured_at` datetime NOT NULL,
  `message_id` int DEFAULT NULL,
  `group_name` varchar(255) DEFAULT NULL,
  `platform` varchar(16) NOT NULL DEFAULT 'signal',
  PRIMARY KEY (`id`),
  KEY `idx_ps_url` (`url`(255)),
  KEY `idx_ps_captured` (`captured_at`),
  KEY `idx_ps_message` (`message_id`),
  FULLTEXT KEY `idx_ft_pages` (`html_content`,`url`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `reactions`
--

DROP TABLE IF EXISTS `reactions`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `reactions` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `reactor_phone` varchar(50) DEFAULT NULL,
  `reactor_uuid` varchar(64) DEFAULT NULL,
  `reactor_name` varchar(255) DEFAULT NULL,
  `target_author_phone` varchar(50) DEFAULT NULL,
  `target_author_uuid` varchar(64) DEFAULT NULL,
  `target_sent_ts` bigint NOT NULL,
  `emoji` varchar(32) NOT NULL,
  `is_remove` tinyint(1) DEFAULT '0',
  `group_id` varchar(128) DEFAULT NULL,
  `group_name` varchar(255) DEFAULT NULL,
  `created_at` datetime(3) DEFAULT CURRENT_TIMESTAMP(3),
  `platform` varchar(16) NOT NULL DEFAULT 'signal',
  `target_platform_user_id` varchar(190) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_reaction` (`reactor_phone`,`target_author_phone`,`target_sent_ts`,`emoji`),
  KEY `idx_rx_target` (`target_author_phone`,`target_sent_ts`),
  KEY `idx_rx_group_time` (`group_id`,`created_at`),
  KEY `idx_rx_reactor` (`reactor_phone`,`created_at`),
  KEY `idx_rx_target_uuid` (`target_author_uuid`,`target_sent_ts`),
  KEY `idx_rx_reactor_uuid` (`reactor_uuid`,`created_at`),
  KEY `idx_rx_target_puid` (`target_platform_user_id`(120))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `remote_deletes`
--

DROP TABLE IF EXISTS `remote_deletes`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `remote_deletes` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `deleter_phone` varchar(50) DEFAULT NULL,
  `deleter_uuid` varchar(64) DEFAULT NULL,
  `deleter_name` varchar(255) DEFAULT NULL,
  `target_sent_ts` bigint NOT NULL,
  `group_id` varchar(128) DEFAULT NULL,
  `group_name` varchar(255) DEFAULT NULL,
  `observed_at` datetime(3) DEFAULT CURRENT_TIMESTAMP(3),
  `platform` varchar(16) NOT NULL DEFAULT 'signal',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_delete` (`deleter_phone`,`target_sent_ts`),
  KEY `idx_rd_target` (`target_sent_ts`),
  KEY `idx_rd_group` (`group_id`,`observed_at`),
  KEY `idx_rd_deleter` (`deleter_phone`,`observed_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `schema_markers`
--

DROP TABLE IF EXISTS `schema_markers`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `schema_markers` (
  `name` varchar(64) NOT NULL,
  `applied_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `sender_profiles`
--

DROP TABLE IF EXISTS `sender_profiles`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `sender_profiles` (
  `id` int NOT NULL AUTO_INCREMENT,
  `sender_phone` varchar(50) NOT NULL,
  `sender_name` varchar(255) DEFAULT NULL,
  `total_messages` int DEFAULT '0',
  `group_count` int DEFAULT '0',
  `url_ratio` float DEFAULT '0',
  `avg_message_length` float DEFAULT '0',
  `posting_hours_json` text,
  `sentiment_dist_json` text,
  `first_seen` datetime DEFAULT NULL,
  `last_seen` datetime DEFAULT NULL,
  `bot_score` float DEFAULT '0',
  `computed_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `platform` varchar(16) NOT NULL DEFAULT 'signal',
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_sp_phone` (`sender_phone`),
  KEY `idx_sp_bot` (`bot_score`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `signal_recipients`
--

DROP TABLE IF EXISTS `signal_recipients`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `signal_recipients` (
  `aci` varchar(64) NOT NULL,
  `pni` varchar(64) DEFAULT NULL,
  `number` varchar(50) DEFAULT NULL,
  `username` varchar(64) DEFAULT NULL,
  `profile_given_name` varchar(255) DEFAULT NULL,
  `profile_family_name` varchar(255) DEFAULT NULL,
  `given_name` varchar(255) DEFAULT NULL,
  `family_name` varchar(255) DEFAULT NULL,
  `nick_name` varchar(255) DEFAULT NULL,
  `profile_about` varchar(512) DEFAULT NULL,
  `unregistered_ts` bigint DEFAULT NULL,
  `last_synced` datetime(3) NOT NULL,
  PRIMARY KEY (`aci`),
  UNIQUE KEY `uq_sr_pni` (`pni`),
  UNIQUE KEY `uq_sr_number` (`number`),
  KEY `idx_sr_username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `tracked_urls`
--

DROP TABLE IF EXISTS `tracked_urls`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `tracked_urls` (
  `id` int NOT NULL AUTO_INCREMENT,
  `url` varchar(2083) NOT NULL,
  `check_interval_hours` int NOT NULL DEFAULT '24',
  `last_checked_at` datetime DEFAULT NULL,
  `last_changed_at` datetime DEFAULT NULL,
  `change_count` int NOT NULL DEFAULT '0',
  `is_active` tinyint(1) NOT NULL DEFAULT '1',
  `consecutive_failures` int NOT NULL DEFAULT '0',
  `added_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_tu_url` (`url`(255)),
  KEY `idx_tu_next_check` (`is_active`,`last_checked_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `url_observations`
--

DROP TABLE IF EXISTS `url_observations`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `url_observations` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `message_id` int DEFAULT NULL,
  `normalized_url` varchar(2083) DEFAULT NULL,
  `domain` varchar(255) DEFAULT NULL,
  `platform` varchar(16) DEFAULT NULL,
  `platform_chat_id` varchar(190) DEFAULT NULL,
  `chat_title` varchar(255) DEFAULT NULL,
  `sender_phone` varchar(64) DEFAULT NULL,
  `platform_user_id` varchar(190) DEFAULT NULL,
  `observed_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_uo_norm` (`normalized_url`(191),`observed_at`),
  KEY `idx_uo_domain` (`domain`,`observed_at`),
  KEY `idx_uo_chat` (`platform`,`platform_chat_id`(120),`observed_at`),
  KEY `idx_uo_message` (`message_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `watchlist_hits`
--

DROP TABLE IF EXISTS `watchlist_hits`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `watchlist_hits` (
  `id` int NOT NULL AUTO_INCREMENT,
  `keyword_id` int NOT NULL,
  `message_id` int NOT NULL,
  `hit_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_wh_keyword` (`keyword_id`),
  KEY `idx_wh_message` (`message_id`),
  KEY `idx_wh_time` (`hit_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `yearly_summaries`
--

DROP TABLE IF EXISTS `yearly_summaries`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `yearly_summaries` (
  `id` int NOT NULL AUTO_INCREMENT,
  `year_start` date NOT NULL,
  `group_name` varchar(255) NOT NULL,
  `summary_text` longtext NOT NULL,
  `monthly_count` int NOT NULL DEFAULT '0',
  `model_used` varchar(128) DEFAULT NULL,
  `generated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `platform` varchar(16) NOT NULL DEFAULT 'signal',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_year_group` (`year_start`,`group_name`),
  KEY `idx_year` (`year_start`),
  KEY `idx_year_group` (`group_name`,`year_start`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

