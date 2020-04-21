import traceback

import numpy as np
import statsmodels.api as sm
import scipy.stats as sps

from maggy.optimizer.abstractoptimizer import AbstractOptimizer
from maggy.trial import Trial

from hops import hdfs

# todo use optimization direction
# todo use transform from search space


class TPE(AbstractOptimizer):
    """
    I have the following class variables from the Abstract Optimizer

    self.searchspace = None # SearchSpace object containing the hp
    self.num_trials = None # just the number of trials int ?
    self.final_store = None # array of finished trials ( can we also access trials that are not finished )

    """

    def __init__(
        self,
        num_warmup_trials=15,
        gamma=0.15,
        num_samples=24,
        bw_estimation="normal_reference",
        bw_factor=3,
        random_fraction=0.1,
    ):
        """

        :param num_warmup_trials: number of random trials at the beginning of experiment
        :type num_warmup_trials: int
        :param gamma: Determines the percentile of configurations that will be used as training data
                for the kernel density estimator, e.g if set to 10 the 10% best configurations will be considered
                for training.
        :type gamma: float
        :param num_samples: number of samples drawn to optimize EI via sampling
        :type num_samples: int
        :param bw_estimation: method used by statsmodel for the bandwidth estimation of the kde. Options are 'normal_reference', 'silvermann', 'scott'
        :type bw_estimation: str
        :param bw_factor: widens the bandwidth for contiuous parameters for proposed points to optimize EI. Higher values favor more exploration
        :type bw_factor: float
        :param random_fraction: fraction of random samples
        :type random_fraction: float
        """
        super().__init__()

        # meta hyper parameters
        self.num_warmup_trails = num_warmup_trials
        self.gamma = gamma
        self.num_samples = num_samples
        self.bw_estimation = bw_estimation
        self.min_bw = 1e-3  # from HpBandSter
        self.bw_factor = bw_factor
        self.random_fraction = random_fraction  # todo findout good default

        # initialize logger
        self.log_file = "hdfs:///Projects/Kai/Logs/tpe.log"
        if not hdfs.exists(self.log_file):
            hdfs.dump("", self.log_file)
        self.fd = hdfs.open_file(self.log_file, flags="w")
        self._log("Initialized Logger")

        # keep track of the model (i.e the kernel density estimators l & g)
        self.model = None
        self.random_warmup_trials = []

    # couldn't this be done in __init__
    def initialize(self):

        # initialize random trials
        random_samples = self.searchspace.get_random_parameter_values(
            self.num_warmup_trails
        )
        for counter, parameters_dict in enumerate(random_samples):
            self.random_warmup_trials.append(
                Trial(parameters_dict, trial_type="optimization")
            )

    def get_suggestion(self, trial=None):
        """Returns Trial instantiated with hparams that maximize the Expected Improvement"""

        try:
            if len(self.final_store) >= self.num_trials:
                self._log(
                    "Finished experiment, ran {}/{} trials".format(
                        len(self.final_store), self.num_trials
                    )
                )
                return None

            self._log("Get Suggestion")

            # first sample randomly to warmup
            if self.random_warmup_trials:
                self._log("Sample Randomly")
                return self.random_warmup_trials.pop()

            # self._log("Start updateing model")

            self._update_model()

            # self._log("Model {}".format(str(self.model)))

            if not self.model or np.random.rand() < self.random_fraction:
                self._log("Sample Randomly")
                hparams = self.searchspace.get_random_parameter_values(1)[0]
                return Trial(hparams)

            # todo optimization direction

            best = -np.inf
            best_sample = None

            kde_good = self.model["good"]
            kde_bad = self.model["bad"]

            # loop through potential samples
            for sample in range(self.num_samples):
                # randomly choose one of the `good` samples as mean
                idx = np.random.randint(0, len(kde_good.data))
                obs = kde_good.data[idx]
                sample_vector = []

                # self._log("Bounds: {}".format(bounds))

                # loop through hparams
                for mean, bw, hparam_spec in zip(
                    obs, kde_good.bw, self.searchspace.items()
                ):

                    if hparam_spec["type"] in ["DOUBLE", "INTEGER"]:
                        # sample for cont. hparams
                        # clip by min bw and multiply by factor to favor more exploration
                        bw = max(bw, self.min_bw) * self.bw_factor

                        # low and high are calculated with bounds of hparamsm, because they are always [0,
                        # 1] for transformed hparams we do not have to incorporate them explicitly
                        # `a, b = (myclip_a - my_mean) / my_std, (myclip_b - my_mean) / my_std`
                        # see: https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.truncnorm.html
                        low = -mean / bw
                        high = (1 - mean) / bw

                        self._log(
                            "Mean: {}, BW: {}, Low: {}, High: {}".format(
                                mean, bw, low, high
                            )
                        )

                        rv = sps.truncnorm.rvs(low, high, loc=mean, scale=bw)
                        sample_vector.append(rv)
                    else:
                        # sample for categorical hparams (sampling logic taken from HpBandSter)
                        if np.random.rand() < (1 - bw):
                            sample_vector.append(mean)
                        else:
                            n_choices = len(hparam_spec["values"])
                            sample_vector.append(np.random.randint(n_choices))

                self._log("Sample Vector: {}".format(sample_vector))

                # calculate EI for current sample
                ei_val = TPE._calculate_ei(sample_vector, kde_good, kde_bad)

                self._log("EI: {}, type: {}".format(ei_val, type(ei_val)))

                if ei_val > best:
                    best = ei_val
                    best_sample = sample_vector

            self._log("Transformed Best Sample: {}".format(best_sample))

            # get original representation of hparams
            best_sample = self._inverse_transform(best_sample)

            # transform sample array to dict
            hparam_names = self.searchspace.keys()
            best_sample_dict = {
                hparam_name: hparam
                for hparam_name, hparam in zip(hparam_names, best_sample)
            }

            self._log("Best Sample {}".format(best_sample_dict))

            return Trial(best_sample_dict)
        except BaseException:
            self._log(traceback.format_exc())
            self.fd.flush()
            self.fd.close()

    def finalize_experiment(self, trials):
        # close logfile
        self.fd.flush()
        self.fd.close()

        return

    # optimizer specific methods

    def _update_model(self):
        """updates model based on all previous observations

        i.e. creating and storing kde for *good* and *bad* observations
        """

        # split trials in good and bad
        # self._log("Split Good and Bad")
        good_trials, bad_trials = self._split_trials()

        # The number of observations must be larger than the number of variables to build the model
        if len(self.searchspace.keys()) >= len(good_trials):
            return None

        # get list of hparams, each item of the list is one observation. each observation is list of hparams
        good_hparams = np.asarray(
            [list(trial.params.values()) for trial in good_trials]
        )
        bad_hparams = np.asarray([list(trial.params.values()) for trial in bad_trials])

        # todo it would be better if transform would be integrated in trial.py

        transformed_good_hparams = np.apply_along_axis(self._transform, 1, good_hparams)
        transformed_bad_hparams = np.apply_along_axis(self._transform, 1, bad_hparams)

        self._log("good: {}".format(good_hparams))
        self._log("normalized good: {}".format(transformed_good_hparams))

        var_type = self._get_statsmodel_vartype()

        good_kde = sm.nonparametric.KDEMultivariate(
            data=transformed_good_hparams, var_type=var_type, bw=self.bw_estimation
        )
        bad_kde = sm.nonparametric.KDEMultivariate(
            data=transformed_bad_hparams, var_type=var_type, bw=self.bw_estimation
        )

        self.model = {"good": good_kde, "bad": bad_kde}

    def _split_trials(self):
        """splits trials in good and bad according to tpe algo

        :return: tuple with list of good trials and bad trials
        :rtype: (list[maggy.Trial], list[maggy.Trial])
        """

        # ToDo I need optimization direction here, for now assume minimize
        metric_history = np.array([trial.final_metric for trial in self.final_store])
        loss_idx_ascending = np.argsort(metric_history)
        n_good = int(np.ceil(self.gamma * len(metric_history)))

        # need to convert list to np.array to work
        # ToDo double check calculation of n_good with HpBandSter
        # For Minimizing
        # good_trails = np.asarray(self.final_store)[np.sort(loss_idx_ascending[:n_good])]
        # bad_trials = np.asarray(self.final_store)[np.sort(loss_idx_ascending[n_good:])]

        # For Maximizing
        good_trails = np.asarray(self.final_store)[
            np.sort(loss_idx_ascending[-n_good:])
        ]
        bad_trials = np.asarray(self.final_store)[np.sort(loss_idx_ascending[:-n_good])]

        return good_trails, bad_trials

    def _get_statsmodel_vartype(self):
        """Returns *statsmodel* type specifier string consisting of the types for each hparam of the searchspace , so for example 'ccuo'.

        :rtype: str
        """

        var_type_string = ""
        for hparam_spec in self.searchspace.items():
            var_type_string += TPE._get_vartype(hparam_spec["type"])

        return var_type_string

    def _transform(self, hparams):
        """Transforms array of hypeparameters for one trial.

        +--------------+------------------------+
        | Hparam Type  | Transformation         |
        +==============+========================+
        | DOUBLE       | Max-Min Normalization  |
        +--------------+------------------------+
        | INTEGER      | Max-Min Normalization  |
        +--------------+------------------------+
        | CATEGORICAL  | Encoding: index in list|
        +--------------+------------------------+

        :param hparams: hparams in original representation for one trial
        :type hparams: 1D np.ndarray
        :return: transformed hparams
        :rtype: np.ndarray[np.float]
        """
        transformed_hparams = []
        # loop through hparams
        for hparam, hparam_spec in zip(hparams, self.searchspace.items()):
            # todo implement transformation for DISCRETE type
            if hparam_spec["type"] == "DOUBLE":
                normalized_hparam = TPE._normalize_scalar(hparam_spec["values"], hparam)
                transformed_hparams.append(normalized_hparam)
            elif hparam_spec["type"] == "INTEGER":
                normalized_hparam = TPE._normalize_integer(
                    hparam_spec["values"], hparam
                )
                transformed_hparams.append(normalized_hparam)
            elif hparam_spec["type"] == "CATEGORICAL":
                encoded_hparam = TPE._encode_categorical(hparam_spec["values"], hparam)
                transformed_hparams.append(encoded_hparam)
            else:
                raise NotImplementedError("Not Implemented other types yet")

        return transformed_hparams

    def _inverse_transform(self, transformed_hparams):
        """Returns array of hparams in same representation as specified when instantiated

        :param transformed_hparams: hparams in transformed representation for one trial
        :type transformed_hparams: 1D np.ndarray
        :return: transformed hparams
        :rtype: np.ndarray
        """
        hparams = []
        for hparam, hparam_spec in zip(transformed_hparams, self.searchspace.items()):
            if hparam_spec["type"] == "DOUBLE":
                value = TPE._inverse_normalize_scalar(hparam_spec["values"], hparam)
                hparams.append(value)
            elif hparam_spec["type"] == "INTEGER":
                value = TPE._inverse_normalize_integer(hparam_spec["values"], hparam)
                hparams.append(value)
            elif hparam_spec["type"] == "CATEGORICAL":
                decoded_hparam = TPE._decode_categorical(hparam_spec["values"], hparam)
                hparams.append(decoded_hparam)
            else:
                raise NotImplementedError("Not Implemented other types yet")

        return hparams

    @staticmethod
    def _encode_categorical(choices, value):
        """Encodes category to integer. The encoding is the list index of the category

        :param choices: possible values of the categorical hparam
        :type choices: list
        :param value: category to encode
        :type value: str
        :return: encoded category
        :rtype: int
        """
        return choices.index(value)

    @staticmethod
    def _decode_categorical(choices, encoded_value):
        """Decodes integer to corresponding category value

        :param choices: possible values of the categorical hparam
        :type choices: list
        :param encoded_value: encoding of category
        :type encoded_value: int
        :return: category value
        :rtype: str
        """
        encoded_value = int(
            encoded_value
        )  # it is possible that value gets casted to np.float by numpy
        return choices[encoded_value]

    @staticmethod
    def _normalize_scalar(bounds, scalar):
        """Returns max-min normalized scalar

        :param bounds: list containing lower and upper bound, e.g.: [-3,3]
        :type bounds: list
        :param scalar: scalar value to be normalized
        :type scalar: float
        :return: normalized scalar
        :rtype: float
        """
        # todo check if bounds is valid and scalar is inside bounds
        scalar = float(scalar)
        scalar = (scalar - bounds[0]) / (bounds[1] - bounds[0])
        scalar = np.minimum(1.0, scalar)
        scalar = np.maximum(0.0, scalar)
        return scalar

    @staticmethod
    def _inverse_normalize_scalar(bounds, normalized_scalar):
        """Returns inverse normalized scalar

        :param bounds: list containing lower and upper bound, e.g.: [-3,3]
        :type bounds: list
        :param normalized_scalar: normalized scalar value
        :type normalized_scalar: float
        :return: original scalar
        :rtype: float
        """

        # todo check if bounds is valid and scalar is inside bounds
        normalized_scalar = float(normalized_scalar)
        normalized_scalar = normalized_scalar * (bounds[1] - bounds[0]) + bounds[0]
        return normalized_scalar

    @staticmethod
    def _normalize_integer(bounds, integer):
        """
        :param bounds: list containing lower and upper bound, e.g.: [-3,3]
        :type bounds: list
        :param integer: value to be normalized
        :type normalized_scalar: int
        :return: normalized value between 0 and 1
        :rtype: float
        """

        integer = int(integer)
        return TPE._normalize_scalar(bounds, integer)

    @staticmethod
    def _inverse_normalize_integer(bounds, scalar):
        """Returns inverse normalized scalar

        :param bounds: list containing lower and upper bound, e.g.: [-3,3]
        :type bounds: list
        :param normalized_scalar: normalized scalar value
        :type normalized_scalar: float
        :return: original integer
        :rtype: int
        """

        x = TPE._inverse_normalize_scalar(bounds, scalar)
        return int(np.round(x))

    @staticmethod
    def _get_vartype(maggy_vartype):
        """Transforms Maggy vartype to statsmodel vartype, e.g. 'DOUBLE' → 'c'

        :param maggy_vartype: maggy type of hparam, e.g. 'DOUBLE'
        :type maggy_vartype: str
        :returns: corresponding vartype of statsmodel
        :rtype: str
        """
        if maggy_vartype == "DOUBLE":
            return "c"
        elif maggy_vartype == "INTEGER":
            return "c"
        elif maggy_vartype == "CATEGORICAL":
            return "u"
        else:
            raise NotImplementedError("Only cont vartypes are implemented yer")

    @staticmethod
    def _calculate_ei(x, kde_good, kde_bad):
        """Returns Expected Improvement for given hparams

        :param x: list of hyperparameters
        :type x: list
        :param kde_good: kde of good observations
        :type kde_good: sm.KDEMultivariate
        :param kde_bad: pdf of kde of bad observations
        :type kde_bad: sm.KDEMultivariate of KDE instance
        :return: expected improvement
        :rtype: float
        """
        return max(1e-32, kde_good.pdf(x)) / max(kde_bad.pdf(x), 1e-32)

    def _log(self, msg):
        self.fd.write((msg + "\n").encode())
