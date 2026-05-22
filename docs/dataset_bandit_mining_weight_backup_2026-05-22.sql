-- mining_weight backup before dataset-steering bandit Tier A go-live
-- generated 2026-05-22T07:52:35.859586Z  rows=17
-- ROLLBACK: flip ENABLE_DATASET_VALUE_BANDIT OFF, then run this file.

UPDATE datasets SET mining_weight = 1.0 WHERE region = 'USA' AND dataset_id = 'analyst4' AND universe = 'TOP3000';
UPDATE datasets SET mining_weight = 1.0 WHERE region = 'USA' AND dataset_id = 'fundamental2' AND universe = 'TOP3000';
UPDATE datasets SET mining_weight = 1.0 WHERE region = 'USA' AND dataset_id = 'fundamental6' AND universe = 'TOP3000';
UPDATE datasets SET mining_weight = 1.0 WHERE region = 'USA' AND dataset_id = 'model16' AND universe = 'TOP3000';
UPDATE datasets SET mining_weight = 1.0 WHERE region = 'USA' AND dataset_id = 'model51' AND universe = 'TOP3000';
UPDATE datasets SET mining_weight = 1.0 WHERE region = 'USA' AND dataset_id = 'model77' AND universe = 'TOP3000';
UPDATE datasets SET mining_weight = 1.0 WHERE region = 'USA' AND dataset_id = 'news12' AND universe = 'TOP3000';
UPDATE datasets SET mining_weight = 1.0 WHERE region = 'USA' AND dataset_id = 'news18' AND universe = 'TOP3000';
UPDATE datasets SET mining_weight = 1.0 WHERE region = 'USA' AND dataset_id = 'option8' AND universe = 'TOP3000';
UPDATE datasets SET mining_weight = 1.0 WHERE region = 'USA' AND dataset_id = 'option9' AND universe = 'TOP3000';
UPDATE datasets SET mining_weight = 1.0 WHERE region = 'USA' AND dataset_id = 'pv1' AND universe = 'TOP3000';
UPDATE datasets SET mining_weight = 1.0 WHERE region = 'USA' AND dataset_id = 'pv13' AND universe = 'TOP3000';
UPDATE datasets SET mining_weight = 1.0 WHERE region = 'USA' AND dataset_id = 'pv96' AND universe = 'TOP3000';
UPDATE datasets SET mining_weight = 1.0 WHERE region = 'USA' AND dataset_id = 'sentiment1' AND universe = 'TOP3000';
UPDATE datasets SET mining_weight = 1.0 WHERE region = 'USA' AND dataset_id = 'socialmedia12' AND universe = 'TOP3000';
UPDATE datasets SET mining_weight = 1.0 WHERE region = 'USA' AND dataset_id = 'socialmedia8' AND universe = 'TOP3000';
UPDATE datasets SET mining_weight = 1.0 WHERE region = 'USA' AND dataset_id = 'univ1' AND universe = 'TOP3000';
