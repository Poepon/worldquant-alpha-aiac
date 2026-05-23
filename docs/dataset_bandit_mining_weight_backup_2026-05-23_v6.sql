-- v6 deploy rollback backup 2026-05-23T18:51:25.311599
-- watermark before deploy: 2026-05-22T21:15:00.005507
UPDATE datasets SET mining_weight=0.03545111555783445 WHERE region='USA' AND dataset_id='analyst4' AND universe='TOP3000';
UPDATE datasets SET mining_weight=0.05193770322694073 WHERE region='USA' AND dataset_id='fundamental2' AND universe='TOP3000';
UPDATE datasets SET mining_weight=0.03136656358969342 WHERE region='USA' AND dataset_id='fundamental6' AND universe='TOP3000';
UPDATE datasets SET mining_weight=0.0274926152429418 WHERE region='USA' AND dataset_id='model16' AND universe='TOP3000';
UPDATE datasets SET mining_weight=0.08692682790013263 WHERE region='USA' AND dataset_id='model51' AND universe='TOP3000';
UPDATE datasets SET mining_weight=0.10707554320944726 WHERE region='USA' AND dataset_id='model77' AND universe='TOP3000';
UPDATE datasets SET mining_weight=0.10838658144751462 WHERE region='USA' AND dataset_id='news12' AND universe='TOP3000';
UPDATE datasets SET mining_weight=0.10262010865782664 WHERE region='USA' AND dataset_id='news18' AND universe='TOP3000';
UPDATE datasets SET mining_weight=0.07767606283222901 WHERE region='USA' AND dataset_id='option8' AND universe='TOP3000';
UPDATE datasets SET mining_weight=0.094968775187114 WHERE region='USA' AND dataset_id='option9' AND universe='TOP3000';
UPDATE datasets SET mining_weight=0.0012752052557666775 WHERE region='USA' AND dataset_id='pv1' AND universe='TOP3000';
UPDATE datasets SET mining_weight=0.1560973148468688 WHERE region='USA' AND dataset_id='pv13' AND universe='TOP3000';
UPDATE datasets SET mining_weight=0.12242453491109397 WHERE region='USA' AND dataset_id='pv96' AND universe='TOP3000';
UPDATE datasets SET mining_weight=0.04321880201078695 WHERE region='USA' AND dataset_id='sentiment1' AND universe='TOP3000';
UPDATE datasets SET mining_weight=0.1175223972478303 WHERE region='USA' AND dataset_id='socialmedia12' AND universe='TOP3000';
UPDATE datasets SET mining_weight=0.550316831946839 WHERE region='USA' AND dataset_id='socialmedia8' AND universe='TOP3000';
UPDATE datasets SET mining_weight=0.4632217459366782 WHERE region='USA' AND dataset_id='univ1' AND universe='TOP3000';
