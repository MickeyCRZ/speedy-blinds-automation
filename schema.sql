-- =====================================================================
-- SpeedyBlinds / Automate Local MySQL Database Schema
-- Database: automate_db
-- =====================================================================

CREATE DATABASE IF NOT EXISTS `automate_db` 
  DEFAULT CHARACTER SET utf8mb4 
  DEFAULT COLLATE utf8mb4_unicode_ci;

USE `automate_db`;

-- ---------------------------------------------------------------------
-- 1. Pending Payments & Charges Queue
-- Stores transactions waiting to be approved and pushed
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `payment_queue` (
  `id` VARCHAR(64) NOT NULL,
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `entry_type` VARCHAR(20) NOT NULL DEFAULT 'payment' COMMENT 'payment or charge',
  `order_number` VARCHAR(64) DEFAULT '',
  `dealer` VARCHAR(100) NOT NULL DEFAULT '',
  `dealer_id` INT NULL COMMENT 'ERP dealer ID',
  `company_key` VARCHAR(50) NOT NULL DEFAULT 'speedy' COMMENT 'speedy, inspira, or houston',
  `amount` DECIMAL(12, 2) NOT NULL DEFAULT 0.00,
  `is_negative` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '1 for negative deduction in sheets',
  `type` VARCHAR(20) NOT NULL DEFAULT 'EFT' COMMENT 'EFT, Cash, CC, Charge',
  `category` VARCHAR(100) DEFAULT '',
  `description` TEXT NULL,
  `sender` VARCHAR(255) DEFAULT '',
  `payment_date` VARCHAR(20) DEFAULT '',
  `target_column_mode` VARCHAR(20) NOT NULL DEFAULT 'primary' COMMENT 'primary, alt, auto',
  `sheet_type` VARCHAR(20) NOT NULL DEFAULT 'new' COMMENT 'new or old',
  `post_to_erp` TINYINT(1) NOT NULL DEFAULT 1,
  `push_to_sheets` TINYINT(1) NOT NULL DEFAULT 1,
  `status` VARCHAR(20) NOT NULL DEFAULT 'pending',
  `erp_price` DECIMAL(12, 2) NULL,
  `sheet_check_json` JSON NULL,
  PRIMARY KEY (`id`),
  KEY `idx_queue_dealer` (`dealer`),
  KEY `idx_queue_order` (`order_number`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------
-- 2. Executed Transactions History
-- Permanent log of all completed ERP ledger & Google Sheet pushes
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `transaction_history` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `transaction_id` VARCHAR(64) NULL,
  `executed_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `entry_type` VARCHAR(20) NOT NULL DEFAULT 'payment',
  `order_number` VARCHAR(64) DEFAULT '',
  `dealer` VARCHAR(100) NOT NULL DEFAULT '',
  `dealer_id` INT NULL,
  `company_key` VARCHAR(50) NOT NULL DEFAULT 'speedy',
  `amount` DECIMAL(12, 2) NOT NULL DEFAULT 0.00,
  `is_negative` TINYINT(1) NOT NULL DEFAULT 0,
  `type` VARCHAR(20) NOT NULL DEFAULT 'EFT',
  `category` VARCHAR(100) DEFAULT '',
  `description` TEXT NULL,
  `sender` VARCHAR(255) DEFAULT '',
  `payment_date` VARCHAR(20) DEFAULT '',
  `post_to_erp` TINYINT(1) NOT NULL DEFAULT 1,
  `push_to_sheets` TINYINT(1) NOT NULL DEFAULT 1,
  `erp_line_id` VARCHAR(64) NULL COMMENT 'ERP balance_account_lines.id',
  `erp_status` VARCHAR(20) NULL COMMENT 'posted, skipped, failed',
  `sheet_status` VARCHAR(50) NULL COMMENT 'matched, appended_red, lump_sum_amber, failed, skipped',
  `sheet_cell` VARCHAR(20) NULL,
  `sheet_row` INT NULL,
  `errors_json` JSON NULL,
  `full_result_json` JSON NULL,
  PRIMARY KEY (`id`),
  KEY `idx_hist_executed` (`executed_at`),
  KEY `idx_hist_order` (`order_number`),
  KEY `idx_hist_dealer` (`dealer`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------
-- 3. Audit Logs (WhatsApp orders, ERP status patches, Sheets writes)
-- Mirrors and expands erp_updates.log in relational database
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `audit_logs` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `logged_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `action_type` VARCHAR(50) NOT NULL COMMENT 'RECORD_PAYMENT, RECORD_CHARGE, STATUS_COMPLETED, PAYMENT_APPENDED_RED, etc.',
  `company` VARCHAR(50) NOT NULL DEFAULT 'Speedy Blinds',
  `order_number` VARCHAR(64) DEFAULT '',
  `target_id` INT NULL COMMENT 'ERP order id or dealer id',
  `http_status` INT NULL,
  `details` TEXT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_audit_logged_at` (`logged_at`),
  KEY `idx_audit_order` (`order_number`),
  KEY `idx_audit_action` (`action_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------
-- 4. Dealers & Sheet Configurations
-- Relational storage of dealer records, aliases, and sheet IDs
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `dealers` (
  `id` INT UNSIGNED NOT NULL AUTO_INCREMENT,
  `company_key` VARCHAR(50) NOT NULL DEFAULT 'speedy',
  `dealer_name` VARCHAR(100) NOT NULL,
  `sheet_id` VARCHAR(128) NOT NULL,
  `sheet_type` ENUM('new', 'old') NOT NULL DEFAULT 'new',
  `erp_dealer_id` INT NULL,
  `aliases_json` JSON NULL,
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_company_dealer` (`company_key`, `dealer_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

