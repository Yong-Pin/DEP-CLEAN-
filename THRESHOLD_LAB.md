# Threshold Lab - How to Use It

## Purpose

Threshold Lab helps justify and refine fall-detection thresholds from collected sensor data. It is an analysis tool, not an automatic safety-controller update mechanism.

## Labelling workflow

On the **Incidents** page, review incidents:

- Confirmed FFH -> `ACTUAL_EVENT` + `FFH` -> Threshold Lab class `FALL`
- Confirmed STF -> `ACTUAL_EVENT` + `STF` -> Threshold Lab class `FALL`
- Confirmed near miss -> `ACTUAL_EVENT` + `NEAR_MISS` -> Threshold Lab class `NO_FALL`
- False alarm -> `FALSE_ALARM` -> Threshold Lab class `NO_FALL`
- Unsure -> excluded from training

The backend also selects ordinary sensor windows away from incidents as background `NO_FALL` examples.

## Minimum readiness

The lab requires:
- at least 5 reviewed incidents;
- at least 3 reviewed FALL examples;
- both FALL and NO_FALL classes in the final training set.

More labelled data is strongly preferred.

## Training

1. Open **Threshold Lab**.
2. Check that the page says **Ready to train**.
3. Enter the dashboard admin password.
4. Select **Train Decision Tree Recommendation**.
5. Review the model metrics and learned split rules.

When enough data exists, Threshold Lab:
- keeps 30% as a stratified holdout set;
- tunes Decision Tree settings using stratified cross-validation on the remaining training data;
- reports the final holdout results.

## Metrics to discuss

For this safety use case, do not discuss accuracy alone.

- **FALL recall**: fraction of true falls detected. Low recall means dangerous false negatives.
- **FALL precision**: fraction of predicted falls that were actually falls. Low precision means excessive false alarms.
- **Balanced accuracy**: useful when FALL and NO_FALL counts are uneven.
- **Confusion matrix**: shows false positives and false negatives directly.

## Using learned thresholds

Do not press a button that automatically replaces live thresholds. Instead:

```text
Reviewed data
-> Decision Tree
-> learned split values
-> controlled validation
-> manual engineering decision
-> edit live thresholds only if justified
```

This separation makes the system safer and the methodology easier to defend during assessment.
