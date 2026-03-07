import numpy as np

def robust_z(x):
    median = np.median(x)
    iqr = np.percentile(x, 75) - np.percentile(x, 25)
    if iqr == 0:
        return np.zeros(len(x))
    return (x - median) / (iqr / 1.349)


def compute_peer_z(df, group_cols, target_cols):
    for col in target_cols:
        df[f"z_{col}"] = df.groupby(group_cols)[col].transform(
            lambda x: robust_z(x.values)
        )
    return df
